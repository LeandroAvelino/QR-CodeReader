"""Pipeline adaptativo de equalização.

Com base na análise da imagem (brilho, contraste, nitidez), gera uma
sequência de variantes progressivamente mais tratadas. O decodificador
tenta ler cada variante na ordem e para na primeira que funcionar.
"""

from typing import Iterator

import cv2
import numpy as np

from .analyzer import ImageAnalysis, MAX_DIMENSION, MIN_DIMENSION

_REPAIR_KERNEL = np.ones((3, 3), np.uint8)


def adjust_gamma(gray: np.ndarray, gamma: float) -> np.ndarray:
    """gamma < 1 clareia a imagem; gamma > 1 escurece."""
    inv = 1.0 / gamma
    table = ((np.arange(256) / 255.0) ** inv * 255).astype(np.uint8)
    return cv2.LUT(gray, table)


def apply_clahe(gray: np.ndarray, clip_limit: float = 3.0) -> np.ndarray:
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(8, 8))
    return clahe.apply(gray)


def unsharp_mask(gray: np.ndarray, strength: float = 1.5) -> np.ndarray:
    blurred = cv2.GaussianBlur(gray, (0, 0), sigmaX=2.0)
    return cv2.addWeighted(gray, 1.0 + strength, blurred, -strength, 0)


def upscale(gray: np.ndarray) -> np.ndarray:
    height, width = gray.shape[:2]
    scale = max(2.0, MIN_DIMENSION / min(width, height))
    return cv2.resize(
        gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
    )


def denoise(gray: np.ndarray) -> np.ndarray:
    return cv2.fastNlMeansDenoising(gray, h=10)


def equalize(gray: np.ndarray, analysis: ImageAnalysis) -> tuple[np.ndarray, list[str]]:
    """Aplica as correções indicadas pela análise e registra cada passo."""
    steps: list[str] = []
    result = gray

    if analysis.is_small:
        result = upscale(result)
        steps.append("upscale")

    if analysis.is_dark:
        result = adjust_gamma(result, gamma=0.6)
        steps.append("gamma_brighten")
    elif analysis.is_bright:
        result = adjust_gamma(result, gamma=1.6)
        steps.append("gamma_darken")

    if analysis.is_low_contrast:
        result = apply_clahe(result)
        steps.append("clahe")

    if analysis.is_blurry:
        result = unsharp_mask(result)
        steps.append("unsharp_mask")

    return result, steps


def downscale(gray: np.ndarray) -> np.ndarray:
    height, width = gray.shape[:2]
    scale = MAX_DIMENSION / min(width, height)
    return cv2.resize(
        gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
    )


def morph_repair(binary: np.ndarray) -> np.ndarray:
    """Erosão leve: engorda os módulos pretos, reconectando falhas de
    impressão desbotada (módulos escuros são 0 na imagem binarizada)."""
    return cv2.erode(binary, _REPAIR_KERNEL)


def _threshold_variants(
    gray: np.ndarray, prefix: list[str]
) -> Iterator[tuple[np.ndarray, list[str]]]:
    """Binarizações com e sem reparo morfológico, da mais barata à mais cara."""
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, otsu = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield otsu, prefix + ["otsu_threshold"]
    yield morph_repair(otsu), prefix + ["otsu_threshold", "morph_repair"]

    for block in (51, 101):
        adaptive = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, blockSize=block, C=10,
        )
        yield adaptive, prefix + [f"adaptive_threshold_{block}"]
        yield morph_repair(adaptive), prefix + [
            f"adaptive_threshold_{block}", "morph_repair"
        ]


def variants(gray: np.ndarray, analysis: ImageAnalysis) -> Iterator[tuple[np.ndarray, list[str]]]:
    """Gera variantes da imagem, da menos à mais agressivamente tratada.

    Cada item é (imagem, lista de passos aplicados).
    """
    # 1. Original, sem tratamento.
    yield gray, []

    # 2. Equalização dirigida pela análise.
    equalized, steps = equalize(gray, analysis)
    if steps:
        yield equalized, steps

    # 3. Filtro de mediana: remove o pontilhado de impressões em meio-tom
    #    (contas e cupons impressos em jato de tinta/offset).
    yield cv2.medianBlur(equalized, 3), steps + ["median_filter"]

    # 4. Fotos grandes: versão reduzida primeiro — mais rápida e tolera
    #    melhor distorções como papel curvado (os módulos ficam mais coesos).
    if analysis.is_large:
        reduced = downscale(equalized)
        yield reduced, steps + ["downscale"]
        yield cv2.medianBlur(reduced, 3), steps + ["downscale", "median_filter"]
        yield from _threshold_variants(reduced, steps + ["downscale"])

    # 5. Binarizações em resolução original, com reparo morfológico para
    #    impressões desbotadas.
    yield from _threshold_variants(equalized, steps)

    # 6. Último recurso: remoção de ruído + CLAHE forte + Otsu + reparo.
    aggressive = denoise(gray)
    aggressive = apply_clahe(aggressive, clip_limit=4.0)
    if analysis.is_small:
        aggressive = upscale(aggressive)
    blurred = cv2.GaussianBlur(aggressive, (3, 3), 0)
    _, aggressive = cv2.threshold(
        blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    yield aggressive, ["denoise", "clahe_strong", "otsu_threshold"]
    yield morph_repair(aggressive), [
        "denoise", "clahe_strong", "otsu_threshold", "morph_repair"
    ]
