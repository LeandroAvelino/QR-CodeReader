"""Decodificação de QR Code com múltiplas tentativas e orçamento de tempo.

Percorre as variantes geradas pelo pipeline de equalização e tenta
decodificar cada uma com uma cadeia de decodificadores, do mais rápido
ao mais pesado: pyzbar, zxing-cpp, OpenCV e WeChatQRCode (CNN com
super-resolução, o mais eficaz para códigos densos ou danificados).
Para na primeira leitura bem-sucedida ou quando o orçamento de tempo
acaba (cancelamento cooperativo: conferido antes de cada variante e de
cada decodificador; nenhuma chamada isolada é interrompida no meio).
"""

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from . import config
from .analyzer import analyze, recapture_hint
from .enhancer import variants

try:
    from pyzbar import pyzbar

    _PYZBAR_AVAILABLE = True
except ImportError:  # pragma: no cover - depende da DLL do zbar no sistema
    _PYZBAR_AVAILABLE = False

try:
    import zxingcpp

    _ZXING_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ZXING_AVAILABLE = False

# Uma thread por leitura: o contêiner tem 1 CPU e as leituras são em série.
cv2.setNumThreads(1)

_CV_DETECTOR = cv2.QRCodeDetector()

_WECHAT_MODEL_DIR = Path(
    os.environ.get(
        "QR_WECHAT_MODELS",
        Path(__file__).resolve().parent.parent / "models" / "wechat_qrcode",
    )
)
_WECHAT_FILES = (
    "detect.prototxt", "detect.caffemodel", "sr.prototxt", "sr.caffemodel"
)
_WECHAT_DETECTOR = None
if hasattr(cv2, "wechat_qrcode") and all(
    (_WECHAT_MODEL_DIR / f).is_file() for f in _WECHAT_FILES
):
    _WECHAT_DETECTOR = cv2.wechat_qrcode.WeChatQRCode(
        *(str(_WECHAT_MODEL_DIR / f) for f in _WECHAT_FILES)
    )

DECODIFICADORES = ("pyzbar", "zxing", "opencv", "wechat")

# Motivos técnicos (enumerados, estáveis) e a orientação em pt-BR para o usuário final.
ORIENTACOES = {
    "sem_qr": (
        "Não encontrei o QR Code na foto. Envie uma nova foto com o QR Code "
        "inteiro aparecendo, de perto, com boa luz e sem reflexo."
    ),
    "qr_ilegivel": (
        "Encontrei o QR Code, mas não consegui lê-lo. Envie uma nova foto mais "
        "de perto, com o cupom esticado, boa luz e sem reflexo."
    ),
    "tempo_esgotado": (
        "Não consegui ler o QR Code a tempo. Envie uma nova foto só do QR Code, "
        "mais de perto, com boa luz e sem reflexo."
    ),
    "imagem_invalida": "Não consegui abrir a imagem. Envie a foto novamente.",
    "tipo_nao_suportado": "Formato de imagem não aceito. Envie a foto em JPEG ou PNG.",
    "muito_grande": "A imagem é grande demais. Envie uma foto comum do celular (até 10 MB).",
    "pixels_demais": "A imagem é grande demais. Envie uma foto comum do celular.",
    "ocupado": "O leitor está ocupado. Tente novamente em alguns segundos.",
    "erro_interno": "Não consegui ler o QR Code agora. Envie a foto novamente em instantes.",
}
MOTIVOS = tuple(ORIENTACOES)


class Prazo:
    """Orçamento de tempo cooperativo."""

    def __init__(self, limite_ms: int):
        self.inicio = time.perf_counter()
        self.limite = limite_ms / 1000.0

    def esgotado(self, estimativa: float = 0.0) -> bool:
        """Esgotado se o tempo acabou ou se a próxima chamada (estimada) não cabe no que resta."""
        return time.perf_counter() - self.inicio + estimativa >= self.limite

    def decorrido_ms(self) -> float:
        return round((time.perf_counter() - self.inicio) * 1000, 2)


@dataclass
class DecodeResult:
    success: bool
    codes: list[str] = field(default_factory=list)
    enhancements_applied: list[str] = field(default_factory=list)
    attempts: int = 0
    decoder: str | None = None
    estrategia: str | None = None
    motivo: str | None = None
    qr_detectado: bool = False
    analysis: dict = field(default_factory=dict)
    suggestion: str | None = None
    processing_time_ms: float = 0.0
    pre_reduzida: bool = False

    @property
    def orientacao(self) -> str | None:
        return ORIENTACOES.get(self.motivo) if self.motivo else None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "codes": self.codes,
            "motivo": self.motivo,
            "orientacao": self.orientacao,
            "qr_detectado": self.qr_detectado,
            "estrategia": self.estrategia,
            "decoder": self.decoder,
            "attempts": self.attempts,
            "enhancements_applied": self.enhancements_applied,
            "analysis": self.analysis,
            "suggestion": self.suggestion,
            "processing_time_ms": self.processing_time_ms,
        }


# Cada decodificador devolve (textos lidos, achou_um_qr_sem_conseguir_ler).

def _decode_pyzbar(image: np.ndarray) -> tuple[list[str], bool]:
    if not _PYZBAR_AVAILABLE:
        return [], False
    results = pyzbar.decode(image, symbols=[pyzbar.ZBarSymbol.QRCODE])
    return [r.data.decode("utf-8", errors="replace") for r in results], False


def _decode_zxing(image: np.ndarray) -> tuple[list[str], bool]:
    if not _ZXING_AVAILABLE:
        return [], False
    results = zxingcpp.read_barcodes(
        image,
        formats=zxingcpp.BarcodeFormat.QRCode,
        try_rotate=True,
        try_downscale=True,
        return_errors=True,
    )
    textos = [r.text for r in results if r.valid]
    detectado = any(not r.valid for r in results)
    return textos, detectado


def _decode_opencv(image: np.ndarray) -> tuple[list[str], bool]:
    try:
        ok, texts, points, _ = _CV_DETECTOR.detectAndDecodeMulti(image)
    except cv2.error:
        return [], False
    if not ok:
        return [], False
    textos = [t for t in texts if t]
    return textos, points is not None and len(textos) < len(texts)


def _decode_wechat(image: np.ndarray) -> tuple[list[str], bool]:
    if _WECHAT_DETECTOR is None:
        return [], False
    texts, _ = _WECHAT_DETECTOR.detectAndDecode(image)
    return [t for t in texts if t], False


_CADEIA = (
    ("pyzbar", _decode_pyzbar),
    ("zxing", _decode_zxing),
    ("opencv", _decode_opencv),
    ("wechat", _decode_wechat),
)


def carregar_cinza(image_bytes: bytes, largura: int, altura: int, lado_max: int) -> tuple[np.ndarray, bool]:
    """Decodifica direto em cinza; foto muito grande já sai reduzida do JPEG/PNG.

    Devolve (imagem, foi_reduzida)."""
    buffer = np.frombuffer(image_bytes, dtype=np.uint8)
    lado = max(largura, altura)
    flag = cv2.IMREAD_GRAYSCALE
    if lado >= lado_max * 4:
        flag = cv2.IMREAD_REDUCED_GRAYSCALE_4
    elif lado >= lado_max * 2:
        flag = cv2.IMREAD_REDUCED_GRAYSCALE_2
    gray = cv2.imdecode(buffer, flag)
    if gray is None:
        raise ValueError("Arquivo não é uma imagem válida.")
    reduzida = flag != cv2.IMREAD_GRAYSCALE
    h, w = gray.shape[:2]
    if max(w, h) > lado_max:
        escala = lado_max / max(w, h)
        gray = cv2.resize(gray, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)
        reduzida = True
    return gray, reduzida


def decode_image(
    image_bytes: bytes,
    largura: int = 0,
    altura: int = 0,
    limite_ms: int | None = None,
    lado_max: int | None = None,
) -> DecodeResult:
    """Decodifica o QR Code de uma imagem, equalizando-a se necessário."""
    prazo = Prazo(limite_ms or config.TEMPO_LIMITE_MS)
    gray, pre_reduzida = carregar_cinza(
        image_bytes, largura, altura, lado_max or config.LADO_MAX
    )
    analysis = analyze(gray)
    # A análise descreve a imagem RECEBIDA (dimensões originais), não a cópia de trabalho.
    analysis_dict = analysis.to_dict()
    if largura and altura:
        analysis_dict["width"], analysis_dict["height"] = largura, altura
    result = DecodeResult(success=False, analysis=analysis_dict, pre_reduzida=pre_reduzida)

    def terminar(motivo: str | None) -> DecodeResult:
        result.motivo = motivo
        if motivo in ("sem_qr", "qr_ilegivel", "tempo_esgotado"):
            result.suggestion = recapture_hint(analysis)
        result.processing_time_ms = prazo.decorrido_ms()
        return result

    # Maior duração já vista de cada decodificador nesta leitura: se a próxima chamada
    # não cabe no tempo que resta, para antes (o prazo não estoura por uma chamada longa).
    maior: dict[str, float] = {}
    for image, steps, nome in variants(gray, analysis):
        if prazo.esgotado():
            return terminar("tempo_esgotado")
        result.attempts += 1
        for decoder_name, decoder in _CADEIA:
            if prazo.esgotado(maior.get(decoder_name, 0.0)):
                return terminar("tempo_esgotado")
            t0 = time.perf_counter()
            codes, detectado = decoder(image)
            maior[decoder_name] = max(maior.get(decoder_name, 0.0), time.perf_counter() - t0)
            result.qr_detectado = result.qr_detectado or detectado
            codes = [c.lstrip("﻿").strip() for c in codes]
            codes = [c for c in codes if c]
            if codes:
                result.success = True
                result.qr_detectado = True
                result.codes = codes
                result.enhancements_applied = (["pre_reducao"] if pre_reduzida else []) + steps
                result.decoder = decoder_name
                result.estrategia = nome
                return terminar(None)

    return terminar("qr_ilegivel" if result.qr_detectado else "sem_qr")
