"""Validação da imagem ANTES de decodificar.

Identifica o tipo real pelos bytes (só JPEG e PNG) e lê largura/altura do
cabeçalho, sem descomprimir, para recusar imagens com pixels demais
("decompression bomb") antes que o OpenCV aloque memória.
"""

import struct

JPEG = "jpeg"
PNG = "png"

_PNG_ASSINATURA = b"\x89PNG\r\n\x1a\n"
# Marcadores SOF (início de quadro) que trazem as dimensões; C4, C8 e CC não são SOF.
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


class ImagemRecusada(ValueError):
    """Imagem recusada antes da decodificação; ``motivo`` é o código técnico."""

    def __init__(self, motivo: str, detalhe: str):
        super().__init__(detalhe)
        self.motivo = motivo


def detectar_tipo(dados: bytes) -> str | None:
    if dados[:3] == b"\xff\xd8\xff":
        return JPEG
    if dados[:8] == _PNG_ASSINATURA:
        return PNG
    return None


def _dimensoes_png(dados: bytes) -> tuple[int, int]:
    # IHDR é obrigatoriamente o primeiro bloco: tamanho(4) "IHDR"(4) largura(4) altura(4)
    if len(dados) < 24 or dados[12:16] != b"IHDR":
        raise ImagemRecusada("imagem_invalida", "PNG sem cabeçalho IHDR.")
    largura, altura = struct.unpack(">II", dados[16:24])
    return largura, altura


def _dimensoes_jpeg(dados: bytes) -> tuple[int, int]:
    i = 2
    n = len(dados)
    while i + 4 <= n:
        if dados[i] != 0xFF:
            i += 1
            continue
        marcador = dados[i + 1]
        if marcador == 0xFF:  # preenchimento
            i += 1
            continue
        if marcador in (0xD8, 0x01) or 0xD0 <= marcador <= 0xD7:
            i += 2
            continue
        if marcador == 0xD9:  # fim da imagem sem SOF
            break
        tamanho = struct.unpack(">H", dados[i + 2:i + 4])[0]
        if tamanho < 2:
            break
        if marcador in _SOF:
            if i + 9 > n:
                break
            altura, largura = struct.unpack(">HH", dados[i + 5:i + 9])
            return largura, altura
        i += 2 + tamanho
    raise ImagemRecusada("imagem_invalida", "JPEG sem cabeçalho de quadro (SOF).")


def validar(dados: bytes, max_pixels: int) -> tuple[str, int, int]:
    """Devolve (tipo, largura, altura) ou levanta ImagemRecusada."""
    if not dados:
        raise ImagemRecusada("imagem_invalida", "Arquivo vazio.")
    tipo = detectar_tipo(dados)
    if tipo is None:
        raise ImagemRecusada(
            "tipo_nao_suportado",
            "Formato não aceito. Envie uma imagem JPEG ou PNG.",
        )
    largura, altura = _dimensoes_png(dados) if tipo == PNG else _dimensoes_jpeg(dados)
    if largura <= 0 or altura <= 0:
        raise ImagemRecusada("imagem_invalida", "Dimensões inválidas no cabeçalho.")
    if largura * altura > max_pixels:
        raise ImagemRecusada(
            "pixels_demais",
            f"Imagem de {largura}x{altura} px excede o limite de {max_pixels} pixels.",
        )
    return tipo, largura, altura
