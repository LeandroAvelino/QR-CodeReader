"""API de leitura de QR Code com equalização adaptativa de imagem.

Proteções (ordem em POST /decode):
1. Leitor ocupado (semáforo cheio) -> 503 "ocupado" sem ler o corpo.
2. Corpo acima do limite (Content-Length ou contagem durante a leitura) -> 413.
3. Tipo real pelos bytes: só JPEG e PNG -> senão 415.
4. Pixels declarados no cabeçalho acima do limite -> 413 (antes de descomprimir).
5. Leitura numa thread separada (o event loop segue respondendo /health),
   com orçamento de tempo cooperativo.
"""

import asyncio
import logging
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from opentelemetry import trace
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from . import config, metricas
from .decoder import ORIENTACOES, decode_image
from .imagem import ImagemRecusada, validar
from .telemetry import setup_telemetry

logger = logging.getLogger("qr_reader")
tracer = trace.get_tracer("qr_reader")

_RE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_RE_EXEC = re.compile(r"^\d{1,20}$")
ORIGENS = {"n8n-cadastro", "manual", "teste"}

app = FastAPI(
    title="QR Code Reader API",
    description=(
        "Recebe uma imagem (JPEG ou PNG), analisa brilho, contraste e nitidez, "
        "equaliza adaptativamente e responde com o conteúdo do QR Code."
    ),
    version=config.VERSAO,
    docs_url="/docs" if config.DOCS else None,
    redoc_url=None,
    openapi_url="/openapi.json" if config.DOCS else None,
)

setup_telemetry(app)

_semaforo = asyncio.Semaphore(config.CONCORRENCIA)


class CorpoGrande(Exception):
    pass


def _contexto(request: Request) -> dict:
    rid = request.headers.get("x-request-id", "")
    origem = request.headers.get("x-origem", "")
    execucao = request.headers.get("x-n8n-execucao", "")
    ctx = {
        "qrcode.requisicao_id": rid if _RE_ID.match(rid) else uuid.uuid4().hex,
        "qrcode.origem": origem if origem in ORIGENS else "desconhecida",
    }
    if _RE_EXEC.match(execucao):
        ctx["n8n.execution.id"] = execucao
    return ctx


def _limite_ms(request: Request) -> int:
    try:
        pedido = int(request.headers.get("x-tempo-limite-ms", ""))
    except ValueError:
        return config.TEMPO_LIMITE_MS
    return max(200, min(pedido, config.TEMPO_LIMITE_MAX_MS))


def _resposta_falha(status: int, motivo: str, detalhe: str, ctx: dict, inicio: float, evento: str = "qrcode.recusa") -> JSONResponse:
    resultado = "erro" if status >= 500 and motivo != "ocupado" else "recusado"
    metricas.REQUISICOES.labels(rota="/decode", classe=metricas.classe_http(status), resultado=resultado).inc()
    metricas.FALHAS.labels(motivo=motivo).inc()
    duracao_ms = round((time.perf_counter() - inicio) * 1000, 2)
    nivel = logging.ERROR if resultado == "erro" else logging.WARNING
    logger.log(nivel, "decode %s", motivo, extra={
        **ctx, "event.name": evento, "qrcode.resultado": resultado, "qrcode.motivo": motivo,
        "qrcode.http_status": status, "qrcode.duracao_ms": duracao_ms,
    })
    corpo = {
        "success": False, "codes": [], "motivo": motivo, "orientacao": ORIENTACOES[motivo],
        "detail": detalhe, "requisicao_id": ctx["qrcode.requisicao_id"], "versao": config.VERSAO,
    }
    headers = {"X-Request-Id": ctx["qrcode.requisicao_id"]}
    if status == 503:
        headers["Retry-After"] = "5"
    return JSONResponse(corpo, status_code=status, headers=headers)


async def _ler_corpo(request: Request, limite: int) -> bytes:
    tamanho = request.headers.get("content-length")
    if tamanho and tamanho.isdigit() and int(tamanho) > limite:
        raise CorpoGrande()
    partes = []
    total = 0
    async for bloco in request.stream():
        total += len(bloco)
        if total > limite:
            raise CorpoGrande()
        partes.append(bloco)
    return b"".join(partes)


async def _extrair_imagem(request: Request, corpo: bytes) -> bytes:
    """Aceita multipart (campo "file") ou o corpo cru (Content-Type image/jpeg|png|octet-stream)."""
    tipo = request.headers.get("content-type", "")
    if not tipo.lower().startswith("multipart/form-data"):
        return corpo

    async def fluxo():
        yield corpo
        yield b""

    parser = MultiPartParser(Headers(raw=request.headers.raw), fluxo(), max_files=1, max_fields=10)
    form = await parser.parse()
    try:
        arquivo = form.get("file")
        if not isinstance(arquivo, UploadFile):
            raise ImagemRecusada("imagem_invalida", 'Envie a imagem no campo "file".')
        return await arquivo.read()
    finally:
        await form.close()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "versao": config.VERSAO}


@app.get("/ready")
def ready() -> JSONResponse:
    from .decoder import _WECHAT_DETECTOR

    pronto = _WECHAT_DETECTOR is not None
    return JSONResponse(
        {"status": "ok" if pronto else "sem_modelos_wechat", "versao": config.VERSAO},
        status_code=200 if pronto else 503,
    )


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/decode")
async def decode(request: Request):
    inicio = time.perf_counter()
    ctx = _contexto(request)

    # 1) ocupado: responde já, sem ler o corpo (o cliente cai no plano B)
    if _semaforo.locked():
        return _resposta_falha(503, "ocupado", "Leitor ocupado; tente novamente.", ctx, inicio)

    async with _semaforo:
        metricas.EM_ANDAMENTO.inc()
        try:
            return await _decodificar(request, ctx, inicio)
        finally:
            metricas.EM_ANDAMENTO.dec()


async def _decodificar(request: Request, ctx: dict, inicio: float):
    try:
        corpo = await _ler_corpo(request, config.MAX_BYTES + config.FOLGA_MULTIPART)
        dados = await _extrair_imagem(request, corpo)
        if len(dados) > config.MAX_BYTES:
            raise CorpoGrande()
        tipo, largura, altura = validar(dados, config.MAX_PIXELS)
    except CorpoGrande:
        return _resposta_falha(413, "muito_grande", f"Arquivo excede o limite de {config.MAX_BYTES} bytes.", ctx, inicio)
    except ImagemRecusada as exc:
        status = {"tipo_nao_suportado": 415, "pixels_demais": 413}.get(exc.motivo, 400)
        return _resposta_falha(status, exc.motivo, str(exc), ctx, inicio)
    except MultiPartException as exc:
        return _resposta_falha(400, "imagem_invalida", f"Multipart inválido: {exc}", ctx, inicio)

    metricas.IMAGEM_BYTES.observe(len(dados))
    metricas.IMAGEM_MEGAPIXELS.observe(largura * altura / 1e6)
    limite = _limite_ms(request)

    with tracer.start_as_current_span("qrcode.decodificar") as span:
        span.set_attribute("qrcode.requisicao_id", ctx["qrcode.requisicao_id"])
        if "n8n.execution.id" in ctx:
            span.set_attribute("n8n.execution.id", ctx["n8n.execution.id"])
        span.set_attribute("qrcode.imagem.bytes", len(dados))
        span.set_attribute("qrcode.imagem.largura", largura)
        span.set_attribute("qrcode.imagem.altura", altura)
        try:
            result = await run_in_threadpool(decode_image, dados, largura, altura, limite)
        except ValueError:
            span.set_attribute("qrcode.resultado", "recusado")
            return _resposta_falha(400, "imagem_invalida", "Arquivo não é uma imagem válida.", ctx, inicio)
        except Exception:  # noqa: BLE001 - qualquer falha vira 500 com motivo estável
            logger.exception("falha interna na leitura", extra={**ctx, "event.name": "qrcode.erro"})
            span.set_attribute("qrcode.resultado", "erro")
            return _resposta_falha(500, "erro_interno", "Falha interna na leitura.", ctx, inicio)
        resultado = "ok" if result.success else result.motivo
        span.set_attribute("qrcode.resultado", resultado)
        span.set_attribute("qrcode.tentativas", result.attempts)
        if result.estrategia:
            span.set_attribute("qrcode.estrategia", result.estrategia)
            span.set_attribute("qrcode.decodificador", result.decoder)

    metricas.REQUISICOES.labels(rota="/decode", classe="2xx", resultado=resultado).inc()
    metricas.DURACAO.labels(resultado=resultado).observe(result.processing_time_ms / 1000)
    metricas.TENTATIVAS.observe(result.attempts)
    if result.success:
        metricas.ESTRATEGIA.labels(estrategia=result.estrategia, decodificador=result.decoder).inc()
    else:
        metricas.FALHAS.labels(motivo=result.motivo).inc()

    diag = result.analysis.get("diagnostics", {})
    extra = {
        **ctx,
        "event.name": "qrcode.leitura",
        "qrcode.resultado": resultado,
        "qrcode.duracao_ms": result.processing_time_ms,
        "qrcode.tamanho_bytes": len(dados),
        "qrcode.tipo": tipo,
        "qrcode.largura": largura,
        "qrcode.altura": altura,
        "qrcode.tentativas": result.attempts,
        "qrcode.qr_detectado": result.qr_detectado,
        "qrcode.diagnostico": ",".join(k for k, v in diag.items() if v),
        "qrcode.pre_reduzida": result.pre_reduzida,
    }
    if result.success:
        extra["qrcode.estrategia"] = result.estrategia
        extra["qrcode.decodificador"] = result.decoder
    else:
        extra["qrcode.motivo"] = result.motivo
    logger.log(logging.INFO if result.motivo != "tempo_esgotado" else logging.WARNING,
               "decode %s", resultado, extra=extra)

    corpo = result.to_dict()
    corpo["requisicao_id"] = ctx["qrcode.requisicao_id"]
    corpo["versao"] = config.VERSAO
    return JSONResponse(corpo, headers={"X-Request-Id": ctx["qrcode.requisicao_id"]})
