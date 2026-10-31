"""API de leitura de QR Code com equalização adaptativa de imagem."""

import logging

from fastapi import FastAPI, File, HTTPException, UploadFile

from .decoder import decode_image
from .telemetry import setup_telemetry

logger = logging.getLogger("qr_reader")

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/bmp",
    "image/tiff",
    # clientes que não informam o tipo: os bytes são validados na decodificação
    "application/octet-stream",
    None,
}

app = FastAPI(
    title="QR Code Reader API",
    description=(
        "Recebe uma imagem, analisa brilho, contraste e nitidez, "
        "equaliza adaptativamente e responde com o conteúdo do QR Code."
    ),
    version="1.0.0",
)

setup_telemetry(app)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/decode")
async def decode(file: UploadFile = File(...)) -> dict:
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Tipo de arquivo não suportado: {file.content_type}. "
                "Envie uma imagem (JPEG, PNG, WebP, BMP ou TIFF)."
            ),
        )

    image_bytes = await file.read()
    if len(image_bytes) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail="Arquivo excede o limite de 10 MB.",
        )
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Arquivo vazio.")

    try:
        result = decode_image(image_bytes)
    except ValueError as exc:
        logger.warning(
            "decode rejeitado: arquivo inválido",
            extra={"qr.filename": file.filename or ""},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info(
        "decode %s", "ok" if result.success else "falhou",
        extra={
            "qr.success": result.success,
            "qr.attempts": result.attempts,
            "qr.decoder": result.decoder or "",
            "qr.enhancements": ",".join(result.enhancements_applied),
            "qr.processing_time_ms": result.processing_time_ms,
            "qr.filename": file.filename or "",
        },
    )
    return result.to_dict()
