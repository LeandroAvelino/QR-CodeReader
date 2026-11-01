"""Funções executadas DENTRO do processo leitor nos testes (importáveis pelo nome)."""

import os
import time

from app.leitor import _decodificar
from app.resultado import DecodeResult


def lento(dados, largura, altura, limite_ms):
    """Leitura real, 2 s mais lenta."""
    time.sleep(2.0)
    return _decodificar(dados, largura, altura, limite_ms)


def trava_ou_morre(dados, largura, altura, limite_ms):
    """Simula uma chamada nativa travada (b"trava") ou o processo morto (b"morre")."""
    if dados == b"trava":
        time.sleep(60)
    if dados == b"morre":
        os._exit(1)
    if dados == b"invalida":
        raise ValueError("Arquivo não é uma imagem válida.")
    return DecodeResult(success=True, codes=["ok"], processing_time_ms=1.0)


def carregar_ok():
    return True
