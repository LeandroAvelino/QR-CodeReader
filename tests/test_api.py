"""Testes da API com imagens de QR Code degradadas.

Gera um QR Code limpo e aplica degradações (escuridão, excesso de brilho,
baixo contraste, desfoque, tamanho reduzido) para verificar que o pipeline
adaptativo recupera a legibilidade.
"""

import io

import cv2
import numpy as np
import pytest
import qrcode
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PAYLOAD = "https://exemplo.com/pedido/12345"


def make_qr_array(payload: str = PAYLOAD, box_size: int = 10) -> np.ndarray:
    qr = qrcode.QRCode(box_size=box_size, border=4)
    qr.add_data(payload)
    qr.make(fit=True)
    pil_image = qr.make_image(fill_color="black", back_color="white")
    return np.array(pil_image.convert("L"), dtype=np.uint8)


def encode_png(gray: np.ndarray) -> bytes:
    ok, buffer = cv2.imencode(".png", gray)
    assert ok
    return buffer.tobytes()


def post_image(image_bytes: bytes, filename: str = "qr.png"):
    return client.post(
        "/decode",
        files={"file": (filename, io.BytesIO(image_bytes), "image/png")},
    )


# --- Degradações -----------------------------------------------------------

def darken(gray: np.ndarray) -> np.ndarray:
    return (gray * 0.25).astype(np.uint8)


def brighten(gray: np.ndarray) -> np.ndarray:
    return np.clip(gray * 0.3 + 190, 0, 255).astype(np.uint8)


def low_contrast(gray: np.ndarray) -> np.ndarray:
    return (gray * 0.15 + 110).astype(np.uint8)


def blur(gray: np.ndarray) -> np.ndarray:
    return cv2.GaussianBlur(gray, (9, 9), 3)


def shrink(gray: np.ndarray) -> np.ndarray:
    return cv2.resize(gray, (120, 120), interpolation=cv2.INTER_AREA)


def noisy_dark(gray: np.ndarray) -> np.ndarray:
    dark = (gray * 0.3).astype(np.float32)
    noise = np.random.default_rng(42).normal(0, 12, gray.shape)
    return np.clip(dark + noise, 0, 255).astype(np.uint8)


def faded_print(gray: np.ndarray) -> np.ndarray:
    """Simula impressão térmica desbotada: módulos pretos afinados e com
    falhas brancas espalhadas."""
    thinned = cv2.dilate(gray, np.ones((2, 2), np.uint8))
    rng = np.random.default_rng(7)
    holes = rng.random(gray.shape) < 0.06
    out = thinned.copy()
    out[holes] = 235
    return cv2.GaussianBlur(out, (3, 3), 0)


def curved_paper(gray: np.ndarray) -> np.ndarray:
    """Simula papel de bobina enrolado: curvatura suave em um eixo, como um
    cupom fiscal fotografado ainda enrolado."""
    height, width = gray.shape
    xx, yy = np.meshgrid(
        np.arange(width, dtype=np.float32),
        np.arange(height, dtype=np.float32),
    )
    map_x = xx + 10 * np.sin(np.pi * yy / height)
    map_y = yy + 6 * np.sin(np.pi * xx / width)
    return cv2.remap(
        gray, map_x, map_y, cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT, borderValue=255,
    )


# --- Testes ----------------------------------------------------------------

def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_decode_clean_image():
    response = post_image(encode_png(make_qr_array()))
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["codes"] == [PAYLOAD]
    assert body["enhancements_applied"] == []
    assert body["suggestion"] is None


@pytest.mark.parametrize(
    "degradation",
    [darken, brighten, low_contrast, blur, shrink, noisy_dark, faded_print, curved_paper],
    ids=[
        "escura", "clara", "baixo_contraste", "borrada", "pequena",
        "escura_com_ruido", "impressao_desbotada", "papel_curvado",
    ],
)
def test_decode_degraded_image(degradation):
    degraded = degradation(make_qr_array())
    response = post_image(encode_png(degraded))
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True, f"Falhou: {body}"
    assert body["codes"] == [PAYLOAD]


def test_rejects_non_image_content_type():
    response = client.post(
        "/decode",
        files={"file": ("doc.txt", io.BytesIO(b"texto"), "text/plain")},
    )
    assert response.status_code == 415


def test_rejects_invalid_image_bytes():
    response = post_image(b"\x89PNG\r\n\x1a\n isso nao e uma imagem de verdade")
    assert response.status_code == 400
    assert response.json()["motivo"] == "imagem_invalida"


def test_image_without_qr_returns_success_false_with_suggestion():
    blank = np.full((400, 400), 200, dtype=np.uint8)
    response = post_image(encode_png(blank))
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["codes"] == []
    assert "Tente novamente" in body["suggestion"]
    assert body["motivo"] == "sem_qr"
    assert body["orientacao"].startswith("Não encontrei o QR Code")


def test_failed_dark_image_suggests_more_light():
    # Imagem escura sem QR legível: a dica deve citar iluminação.
    dark_noise = (
        np.random.default_rng(1).normal(40, 10, (400, 400))
        .clip(0, 255).astype(np.uint8)
    )
    response = post_image(encode_png(dark_noise))
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert "iluminado" in body["suggestion"]
