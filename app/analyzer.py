"""Análise de qualidade da imagem.

Mede brilho, contraste e nitidez para que o pipeline de equalização
aplique apenas as correções necessárias.
"""

from dataclasses import dataclass, asdict

import cv2
import numpy as np

# Limiares calibrados para imagens de QR Code em escala de cinza (0-255).
BRIGHTNESS_LOW = 90.0    # abaixo disso a imagem é considerada escura
BRIGHTNESS_HIGH = 180.0  # acima disso a imagem é considerada clara demais
CONTRAST_LOW = 50.0      # desvio-padrão mínimo para um bom contraste
SHARPNESS_LOW = 100.0    # variância do Laplaciano abaixo disso indica desfoque
MIN_DIMENSION = 300      # lado mínimo para decodificação confiável
MAX_DIMENSION = 1400     # acima disso vale tentar uma versão reduzida


@dataclass
class ImageAnalysis:
    brightness: float
    contrast: float
    sharpness: float
    width: int
    height: int

    @property
    def is_dark(self) -> bool:
        return self.brightness < BRIGHTNESS_LOW

    @property
    def is_bright(self) -> bool:
        return self.brightness > BRIGHTNESS_HIGH

    @property
    def is_low_contrast(self) -> bool:
        return self.contrast < CONTRAST_LOW

    @property
    def is_blurry(self) -> bool:
        return self.sharpness < SHARPNESS_LOW

    @property
    def is_small(self) -> bool:
        return min(self.width, self.height) < MIN_DIMENSION

    @property
    def is_large(self) -> bool:
        return min(self.width, self.height) > MAX_DIMENSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update(
            diagnostics={
                "dark": self.is_dark,
                "bright": self.is_bright,
                "low_contrast": self.is_low_contrast,
                "blurry": self.is_blurry,
                "small": self.is_small,
                "large": self.is_large,
            }
        )
        return data


def recapture_hint(analysis: "ImageAnalysis") -> str:
    """Monta uma dica de recaptura a partir do diagnóstico da imagem.

    Usada quando nenhuma variante foi decodificada: orienta o usuário a
    tirar uma foto nova corrigindo o que o diagnóstico apontou.
    """
    tips: list[str] = []
    if analysis.is_dark:
        tips.append("fotografe em um ambiente mais iluminado")
    if analysis.is_bright:
        tips.append("evite luz direta e reflexos sobre o código")
    if analysis.is_low_contrast:
        tips.append("aumente o contraste da cena (fundo claro, boa iluminação)")
    if analysis.is_blurry:
        tips.append("segure a câmera firme e aguarde o foco antes de capturar")
    if analysis.is_small:
        tips.append("aproxime a câmera até o QR Code preencher a maior parte do quadro")

    if not tips:
        # Métricas globais boas: o problema é local (impressão danificada,
        # papel amassado/curvado ou código muito denso para a resolução).
        tips.append(
            "aproxime a câmera até o QR Code preencher o quadro, "
            "estique o papel e fotografe de frente, com luz uniforme"
        )
    return "Não foi possível ler o código. Tente novamente: " + "; ".join(tips) + "."


def analyze(gray: np.ndarray) -> ImageAnalysis:
    """Extrai métricas de qualidade de uma imagem em escala de cinza."""
    brightness = float(gray.mean())
    contrast = float(gray.std())
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    height, width = gray.shape[:2]
    return ImageAnalysis(
        brightness=round(brightness, 2),
        contrast=round(contrast, 2),
        sharpness=round(sharpness, 2),
        width=width,
        height=height,
    )
