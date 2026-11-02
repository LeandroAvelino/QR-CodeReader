"""Leitura em processo separado, com prazo rígido.

O orçamento de tempo do decoder é cooperativo: é conferido entre uma chamada
e outra, mas uma chamada de decodificador (código nativo) não pode ser
interrompida no meio. Para que o prazo valha de verdade, a leitura roda num
processo filho ("processo leitor"):

- o filho carrega o OpenCV e os modelos uma vez e atende leituras em série;
- a API espera a resposta até ``limite + folga``; passou disso, encerra o
  filho (SIGKILL) e inicia outro, e a requisição recebe ``tempo_esgotado``;
- se o filho morrer (ex.: falta de memória), a requisição recebe
  ``erro_interno`` e outro filho é iniciado.

O processo da API não importa o OpenCV: só repassa bytes e recebe o
``DecodeResult``.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import queue
import signal
import threading
import time
from typing import Callable

from . import metricas
from .resultado import DecodeResult

logger = logging.getLogger("qr_reader")

# spawn: o filho começa limpo (sem as threads da API/OpenTelemetry herdadas por fork).
_CTX = mp.get_context("spawn")

# Tempo máximo para um processo novo ficar pronto (importar OpenCV e carregar modelos).
PRONTO_MAX_S = 60.0


class TempoEsgotado(Exception):
    """O processo leitor não respondeu dentro do prazo rígido."""


class FalhaLeitor(Exception):
    """O processo leitor morreu ou não ficou pronto."""


def _decodificar(dados: bytes, largura: int, altura: int, limite_ms: int) -> DecodeResult:
    from .decoder import decode_image

    return decode_image(dados, largura, altura, limite_ms)


def _carregar_padrao() -> bool:
    from .decoder import _WECHAT_DETECTOR

    return _WECHAT_DETECTOR is not None


def _laco(conn, alvo: Callable, carregar: Callable[[], bool]) -> None:
    """Corpo do processo filho."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C/SIGTERM são tratados pelo pai
    try:
        conn.send(("pronto", bool(carregar())))
    except Exception as exc:  # noqa: BLE001
        conn.send(("falha_carga", type(exc).__name__))
        return
    while True:
        try:
            pedido = conn.recv()
        except (EOFError, OSError):
            return
        if pedido is None:
            return
        try:
            conn.send(("ok", alvo(*pedido)))
        except ValueError as exc:
            conn.send(("invalida", str(exc)))
        except Exception as exc:  # noqa: BLE001
            conn.send(("erro", type(exc).__name__))


class _Processo:
    def __init__(self, alvo: Callable, carregar: Callable[[], bool]):
        self.conn, filho = _CTX.Pipe()
        self.proc = _CTX.Process(
            target=_laco, args=(filho, alvo, carregar), name="qrcode-leitor", daemon=True
        )
        self.proc.start()
        filho.close()
        self.pronto = threading.Event()
        self.wechat = False
        self.falhou = False
        # Só esta thread lê o pipe até o "pronto"; depois, só quem estiver lendo.
        threading.Thread(target=self._esperar_pronto, daemon=True, name="qrcode-leitor-pronto").start()

    def _esperar_pronto(self) -> None:
        try:
            if self.conn.poll(PRONTO_MAX_S):
                tipo, valor = self.conn.recv()
                if tipo == "pronto":
                    self.wechat = valor
                else:
                    self.falhou = True
                    logger.error("processo leitor não carregou", extra={"event.name": "qrcode.erro", "qrcode.erro": valor})
            else:
                self.falhou = True
        except (EOFError, OSError):
            self.falhou = True
        self.pronto.set()

    def executar(self, args: tuple, prazo_s: float) -> DecodeResult:
        self.conn.send(args)
        if not self.conn.poll(prazo_s):
            raise TempoEsgotado()
        try:
            tipo, valor = self.conn.recv()
        except (EOFError, OSError) as exc:
            raise FalhaLeitor("processo leitor encerrou durante a leitura") from exc
        if tipo == "ok":
            return valor
        if tipo == "invalida":
            raise ValueError(valor)
        raise RuntimeError(f"falha no processo leitor: {valor}")

    def encerrar(self) -> None:
        if self.proc.is_alive():
            self.proc.kill()
        self.proc.join(timeout=2)
        self.conn.close()


class Leitor:
    """Conjunto de ``n`` processos leitores (o semáforo da API garante no máximo ``n`` leituras)."""

    def __init__(
        self,
        n: int = 1,
        alvo: Callable = _decodificar,
        carregar: Callable[[], bool] = _carregar_padrao,
    ):
        self.n = n
        self.alvo = alvo
        self.carregar = carregar
        self._livres: queue.SimpleQueue[_Processo] = queue.SimpleQueue()
        self._todos: list[_Processo] = []
        self._trava = threading.Lock()
        self._iniciado = False

    def iniciar(self) -> None:
        with self._trava:
            if self._iniciado:
                return
            for _ in range(self.n):
                p = _Processo(self.alvo, self.carregar)
                self._todos.append(p)
                self._livres.put(p)
            self._iniciado = True

    def _substituir(self, velho: _Processo) -> _Processo:
        velho.encerrar()
        novo = _Processo(self.alvo, self.carregar)
        with self._trava:
            self._todos = [p for p in self._todos if p is not velho] + [novo]
        return novo

    def pronto(self) -> tuple[bool, bool]:
        """(algum processo pronto, WeChat carregado). Não bloqueia."""
        self.iniciar()
        prontos = [p for p in list(self._todos) if p.pronto.is_set() and not p.falhou]
        return bool(prontos), any(p.wechat for p in prontos)

    def memoria_bytes(self) -> float:
        """RSS somado dos processos leitores vivos (Linux, via /proc; 0 se indisponível)."""
        total = 0
        for p in list(self._todos):
            try:
                with open(f"/proc/{p.proc.pid}/status", encoding="ascii") as f:
                    for linha in f:
                        if linha.startswith("VmRSS:"):
                            total += int(linha.split()[1]) * 1024
                            break
            except (OSError, ValueError, TypeError):
                continue
        return float(total)

    def ler(self, dados: bytes, largura: int, altura: int, limite_ms: int, folga_ms: int) -> DecodeResult:
        """Bloqueante (chamar numa thread). Prazo total: ``limite_ms + folga_ms``,
        contando a espera por um processo recém-iniciado."""
        self.iniciar()
        inicio = time.monotonic()
        limite_s = limite_ms / 1000.0
        proc = self._livres.get()
        try:
            if not proc.pronto.wait(PRONTO_MAX_S) or proc.falhou:
                metricas.REINICIOS.labels(motivo="falha").inc()
                proc = self._substituir(proc)
                raise FalhaLeitor("processo leitor não ficou pronto")
            restante_s = limite_s - (time.monotonic() - inicio)
            if restante_s < 0.2:
                # Esperou um processo recém-iniciado e não sobrou tempo; o processo segue bom.
                raise TempoEsgotado()
            try:
                return proc.executar(
                    (dados, largura, altura, int(restante_s * 1000)), restante_s + folga_ms / 1000.0
                )
            except TempoEsgotado:
                metricas.REINICIOS.labels(motivo="prazo").inc()
                logger.warning("processo leitor encerrado por prazo", extra={"event.name": "qrcode.leitor_reiniciado", "qrcode.motivo": "prazo"})
                proc = self._substituir(proc)
                raise
            except FalhaLeitor:
                metricas.REINICIOS.labels(motivo="falha").inc()
                logger.error("processo leitor morreu", extra={"event.name": "qrcode.leitor_reiniciado", "qrcode.motivo": "falha"})
                proc = self._substituir(proc)
                raise
        finally:
            self._livres.put(proc)

    def encerrar(self) -> None:
        with self._trava:
            for p in self._todos:
                try:
                    p.conn.send(None)
                except (OSError, ValueError):
                    pass
                p.encerrar()
            self._todos = []
            self._livres = queue.SimpleQueue()
            self._iniciado = False
