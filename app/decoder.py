"""Decodificação de QR Code com múltiplas tentativas.

Percorre as variantes geradas pelo pipeline de equalização e tenta
decodificar cada uma com uma cadeia de decodificadores, do mais rápido
ao mais pesado: pyzbar, zxing-cpp, OpenCV e WeChatQRCode (CNN com
super-resolução, o mais eficaz para códigos densos ou danificados).
Para na primeira leitura bem-sucedida.
"""

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

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


@dataclass
class DecodeResult:
    success: bool
    codes: list[str] = field(default_factory=list)
    enhancements_applied: list[str] = field(default_factory=list)
    attempts: int = 0
    decoder: str | None = None
    analysis: dict = field(default_factory=dict)
    suggestion: str | None = None
    processing_time_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "codes": self.codes,
            "analysis": self.analysis,
            "enhancements_applied": self.enhancements_applied,
            "attempts": self.attempts,
            "decoder": self.decoder,
            "suggestion": self.suggestion,
            "processing_time_ms": self.processing_time_ms,
        }


def _decode_pyzbar(image: np.ndarray) -> list[str]:
    if not _PYZBAR_AVAILABLE:
        return []
    results = pyzbar.decode(image, symbols=[pyzbar.ZBarSymbol.QRCODE])
    return [r.data.decode("utf-8", errors="replace") for r in results]


def _decode_zxing(image: np.ndarray) -> list[str]:
    if not _ZXING_AVAILABLE:
        return []
    results = zxingcpp.read_barcodes(
        image,
        formats=zxingcpp.BarcodeFormat.QRCode,
        try_rotate=True,
        try_downscale=True,
    )
    return [r.text for r in results if r.valid]


def _decode_opencv(image: np.ndarray) -> list[str]:
    try:
        ok, texts, _, _ = _CV_DETECTOR.detectAndDecodeMulti(image)
    except cv2.error:
        return []
    if not ok:
        return []
    return [t for t in texts if t]


def _decode_wechat(image: np.ndarray) -> list[str]:
    if _WECHAT_DETECTOR is None:
        return []
    texts, _ = _WECHAT_DETECTOR.detectAndDecode(image)
    return [t for t in texts if t]


def decode_image(image_bytes: bytes) -> DecodeResult:
    """Decodifica o QR Code de uma imagem, equalizando-a se necessário."""
    start = time.perf_counter()

    buffer = np.frombuffer(image_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError("Arquivo não é uma imagem válida.")

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    analysis = analyze(gray)

    result = DecodeResult(success=False, analysis=analysis.to_dict())

    for image, steps in variants(gray, analysis):
        result.attempts += 1
        for decoder_name, decoder in (
            ("pyzbar", _decode_pyzbar),
            ("zxing", _decode_zxing),
            ("opencv", _decode_opencv),
            ("wechat", _decode_wechat),
        ):
            codes = [c.lstrip("﻿").strip() for c in decoder(image)]
            codes = [c for c in codes if c]
            if codes:
                result.success = True
                result.codes = codes
                result.enhancements_applied = steps
                result.decoder = decoder_name
                result.processing_time_ms = round(
                    (time.perf_counter() - start) * 1000, 2
                )
                return result

    result.suggestion = recapture_hint(analysis)
    result.processing_time_ms = round((time.perf_counter() - start) * 1000, 2)
    return result
