"""Testes das proteções de produção: limites, tipos, prazo, concorrência, logs e métricas."""

import asyncio
import io
import json
import logging
import struct
import threading
import time
import zlib

import cv2
import numpy as np
import pytest
import qrcode
from fastapi.testclient import TestClient

from app import config, imagem, main
from app.decoder import MOTIVOS, ORIENTACOES, decode_image
from app.enhancer import NOMES_VARIANTES
from app.telemetry import FormatoJson

NFCE = (
    "https://www.nfce.fazenda.sp.gov.br/qrcode?p="
    "35260961585865222480650140000284631859397372|2|1|1|0A1B2C3D4E5F60718293A4B5C6D7E8F901234567"
)


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c


def qr_cinza(payload: str = NFCE, box: int = 6) -> np.ndarray:
    q = qrcode.QRCode(box_size=box, border=4, error_correction=qrcode.constants.ERROR_CORRECT_L)
    q.add_data(payload)
    q.make(fit=True)
    return np.array(q.make_image().convert("L"), dtype=np.uint8)


def foto_grande(com_qr: bool = True, largura: int = 1920, altura: int = 2560) -> bytes:
    """Foto sintética 1920x2560 de "cupom": fundo com ruído e QR no meio."""
    rng = np.random.default_rng(3)
    base = rng.normal(205, 18, (altura, largura)).clip(0, 255).astype(np.uint8)
    if com_qr:
        qr = qr_cinza(box=9)
        h, w = qr.shape
        y, x = (altura - h) // 2, (largura - w) // 2
        base[y:y + h, x:x + w] = np.where(qr < 128, 30, 235)
    ok, buf = cv2.imencode(".jpg", base, [cv2.IMWRITE_JPEG_QUALITY, 75])
    assert ok
    return buf.tobytes()


def png(gray: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", gray)
    assert ok
    return buf.tobytes()


def png_falso(largura: int, altura: int) -> bytes:
    """PNG com cabeçalho IHDR declarando dimensões enormes (bomba de descompressão)."""
    ihdr = struct.pack(">IIBBBBB", largura, altura, 8, 0, 0, 0, 0)
    bloco = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    return b"\x89PNG\r\n\x1a\n" + bloco + b"\x00" * 64


def jpeg_falso(largura: int, altura: int) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, altura, largura, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + sof + b"\x00" * 64 + b"\xff\xd9"


# --- validação da imagem ----------------------------------------------------

def test_dimensoes_do_cabecalho():
    ok, jpg = cv2.imencode(".jpg", np.zeros((123, 456), np.uint8))
    assert imagem.validar(jpg.tobytes(), 10**8) == ("jpeg", 456, 123)
    assert imagem.validar(png(np.zeros((77, 88), np.uint8)), 10**8) == ("png", 88, 77)


@pytest.mark.parametrize("ext", [".bmp", ".tiff", ".webp"])
def test_recusa_formatos_fora_de_jpeg_png(client, ext):
    ok, buf = cv2.imencode(ext, qr_cinza())
    assert ok
    # mesmo mentindo o content-type, o tipo real (bytes mágicos) decide
    r = client.post("/decode", files={"file": ("x.jpg", io.BytesIO(buf.tobytes()), "image/jpeg")})
    assert r.status_code == 415
    assert r.json()["motivo"] == "tipo_nao_suportado"


@pytest.mark.parametrize("falso", [png_falso(30000, 30000), jpeg_falso(20000, 20000)])
def test_recusa_bomba_de_pixels_sem_descomprimir(client, falso):
    r = client.post("/decode", content=falso, headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 413
    body = r.json()
    assert body["motivo"] == "pixels_demais"
    assert body["orientacao"] == ORIENTACOES["pixels_demais"]


def test_recusa_corpo_grande(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_BYTES", 2048)
    monkeypatch.setattr(config, "FOLGA_MULTIPART", 0)
    r = client.post("/decode", content=b"\xff\xd8\xff" + b"0" * 5000, headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 413
    assert r.json()["motivo"] == "muito_grande"


def test_multipart_sem_campo_file(client):
    r = client.post("/decode", files={"outro": ("a.png", io.BytesIO(png(qr_cinza())), "image/png")})
    assert r.status_code == 400
    assert r.json()["motivo"] == "imagem_invalida"


# --- leitura ------------------------------------------------------------------

def test_corpo_cru_jpeg_e_request_id(client):
    ok, jpg = cv2.imencode(".jpg", qr_cinza(box=8), [cv2.IMWRITE_JPEG_QUALITY, 90])
    r = client.post("/decode", content=jpg.tobytes(), headers={
        "Content-Type": "image/jpeg", "X-Request-Id": "cad-1-exec-99", "X-Origem": "teste"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["codes"] == [NFCE]
    assert body["motivo"] is None and body["orientacao"] is None
    assert body["estrategia"] in NOMES_VARIANTES
    assert body["requisicao_id"] == "cad-1-exec-99"
    assert r.headers["x-request-id"] == "cad-1-exec-99"


def test_request_id_invalido_vira_uuid(client):
    r = client.post("/decode", content=png(qr_cinza()), headers={
        "Content-Type": "image/png", "X-Request-Id": "não vale; drop table"})
    assert r.status_code == 200
    assert len(r.json()["requisicao_id"]) == 32


def test_foto_grande_1920x2560_usa_pre_reducao():
    dados = foto_grande()
    res = decode_image(dados, 1920, 2560, limite_ms=15000)
    assert res.success, res.to_dict()
    assert res.codes == [NFCE]
    assert res.pre_reduzida and res.enhancements_applied[0] == "pre_reducao"
    assert res.analysis["width"] == 1920 and res.analysis["height"] == 2560


def test_prazo_esgotado_e_respeitado():
    dados = foto_grande(com_qr=False)
    inicio = time.perf_counter()
    res = decode_image(dados, 1920, 2560, limite_ms=300)
    gasto = time.perf_counter() - inicio
    assert res.success is False
    assert res.motivo == "tempo_esgotado"
    assert res.orientacao == ORIENTACOES["tempo_esgotado"]
    # prazo cooperativo: uma chamada em andamento termina antes de parar (folga de 2,5 s)
    assert gasto < 0.3 + 2.5


def test_qr_detectado_mas_ilegivel():
    """Localizadores intactos e área de dados destruída: deve dizer qr_ilegivel."""
    qr = qr_cinza(box=8)
    h, w = qr.shape
    borda = 4 * 8 + 8 * 8  # margem + localizador (7 módulos) + separador
    rng = np.random.default_rng(11)
    miolo = qr[borda:h - borda, borda:w - borda]
    qr[borda:h - borda, borda:w - borda] = np.where(rng.random(miolo.shape) < 0.5, 0, 255)
    res = decode_image(png(qr), w, h, limite_ms=15000)
    assert res.success is False
    assert res.motivo in ("qr_ilegivel", "sem_qr")
    # o caso esperado é qr_ilegivel; se algum decodificador não reportar a detecção,
    # o teste abaixo documenta o comportamento real
    assert res.motivo == "qr_ilegivel", res.to_dict()


def test_motivos_tem_orientacao_estavel():
    for motivo in MOTIVOS:
        assert ORIENTACOES[motivo].endswith(".")


# --- concorrência, logs, métricas ---------------------------------------------

def test_ocupado_responde_503_sem_ler_corpo(client, monkeypatch):
    monkeypatch.setattr(main, "_semaforo", asyncio.Semaphore(0))
    r = client.post("/decode", content=png(qr_cinza()), headers={"Content-Type": "image/png"})
    assert r.status_code == 503
    assert r.json()["motivo"] == "ocupado"
    assert r.headers["retry-after"] == "5"


def test_health_responde_durante_leitura_longa(client, monkeypatch):
    original = main.decode_image

    def lento(*args, **kwargs):
        time.sleep(2.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(main, "decode_image", lento)
    dados = png(qr_cinza())
    resultado = {}

    def chamar():
        resultado["r"] = client.post("/decode", content=dados, headers={"Content-Type": "image/png"})

    t = threading.Thread(target=chamar)
    t.start()
    time.sleep(0.5)
    inicio = time.perf_counter()
    h = client.get("/health")
    gasto_health = time.perf_counter() - inicio
    ocupado = client.post("/decode", content=dados, headers={"Content-Type": "image/png"})
    t.join()
    assert h.status_code == 200 and gasto_health < 0.5
    assert ocupado.status_code == 503
    assert resultado["r"].status_code == 200 and resultado["r"].json()["success"] is True


def test_log_sem_conteudo_do_qr_nem_nome_de_arquivo(client, caplog):
    caplog.set_level(logging.INFO, logger="qr_reader")
    nome = "cupom_CPF_12345678900.png"
    r = client.post("/decode", files={"file": (nome, io.BytesIO(png(qr_cinza())), "image/png")},
                    headers={"X-Request-Id": "teste-log"})
    assert r.json()["success"] is True
    linhas = [FormatoJson().format(rec) for rec in caplog.records if rec.name == "qr_reader"]
    assert linhas, "nenhum log de leitura"
    evento = json.loads(linhas[-1])
    assert evento["evento"] == "qrcode.leitura"
    assert evento["requisicao_id"] == "teste-log"
    assert evento["resultado"] == "ok"
    texto = "\n".join(linhas)
    assert "nfce.fazenda" not in texto and "3526096158" not in texto
    assert "12345678900" not in texto and "cupom_" not in texto


def test_metrics_expoe_contadores(client):
    client.post("/decode", content=png(qr_cinza()), headers={"Content-Type": "image/png"})
    m = client.get("/metrics").text
    for nome in ("qrcode_requisicoes_total", "qrcode_leitura_duracao_seconds_bucket",
                 "qrcode_estrategia_total", "qrcode_imagem_bytes_bucket", "qrcode_build_info",
                 "qrcode_em_andamento"):
        assert nome in m, nome
