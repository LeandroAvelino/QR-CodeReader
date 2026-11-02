"""Benchmark do leitor: tempo, sucesso e pico de memória por imagem.

Uso (dentro da imagem de teste, com os mesmos limites da produção):

  docker run --rm --cpus 1 --memory 768m -v <fotos>:/fotos:ro -v <saida>:/saida \
      qrcode-reader-test python -m bench.benchmark /fotos /saida

- Gera fotos sintéticas 1920x2560 em /saida/sinteticas (QR de NFC-e de SP FICTÍCIO).
- Cada imagem é lida num processo novo (pico de memória isolado), com o prazo de
  produção (8 s) e com prazo "sem limite" (60 s, para ver o pior caso do pipeline).
- Não imprime o conteúdo dos QR Codes (fotos reais trazem a chave do cupom).
- /saida/resultado.json fica fora do repositório (bench/saida/ está no .gitignore).
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

FICTICIO = (
    "https://www.nfce.fazenda.sp.gov.br/qrcode?p="
    "35261012345678000195650010000123451123456780|2|1|1|5F3A9C0B7D2E4A6C8B1D3F5E7A9C0B2D4F6E8A1C"
)
RE_SP = re.compile(r"^https://www\.nfce\.fazenda\.sp\.gov\.br/", re.I)
W, H = 1920, 2560


def _qr(box: int) -> np.ndarray:
    import qrcode

    q = qrcode.QRCode(box_size=box, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(FICTICIO)
    q.make(fit=True)
    return np.array(q.make_image().convert("L"), dtype=np.uint8)


def _papel(rng, media=205, desvio=14) -> np.ndarray:
    base = rng.normal(media, desvio, (H, W)).astype(np.float32)
    # textura de papel térmico: listras horizontais fracas
    base += (np.sin(np.arange(H) / 3.0)[:, None] * 4).astype(np.float32)
    return base


def _colar(base: np.ndarray, qr: np.ndarray, x: int, y: int, escuro=35, claro=232) -> np.ndarray:
    h, w = qr.shape
    base[y:y + h, x:x + w] = np.where(qr < 128, escuro, claro)
    return base


def _texto_falso(base: np.ndarray, rng) -> np.ndarray:
    # linhas de "texto" do cupom (blocos escuros), para o detector ter distração
    for linha in range(80, H - 80, 46):
        x = 120
        while x < W - 200:
            largura = int(rng.integers(12, 60))
            base[linha:linha + 18, x:x + largura] -= rng.uniform(60, 120)
            x += largura + int(rng.integers(8, 30))
    return base


def _jpeg(img: np.ndarray, caminho: Path, qualidade=75) -> None:
    ok, buf = cv2.imencode(".jpg", img.clip(0, 255).astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, qualidade])
    assert ok
    caminho.write_bytes(buf.tobytes())


def gerar_sinteticas(pasta: Path) -> list[Path]:
    pasta.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(2026)
    saida = []

    def novo(nome):
        p = pasta / f"{nome}.jpg"
        saida.append(p)
        return p

    # 1. limpa, QR grande no meio do cupom
    b = _texto_falso(_papel(rng), rng)
    _jpeg(_colar(b, _qr(10), 560, 1500), novo("s01_limpa"))
    # 2. QR pequeno (módulo de 4 px) num canto: foto do cupom inteiro de longe
    b = _texto_falso(_papel(rng), rng)
    _jpeg(_colar(b, _qr(4), 1300, 2100), novo("s02_qr_pequeno"))
    # 3. desfocada (foto tremida)
    b = _texto_falso(_papel(rng), rng)
    b = cv2.GaussianBlur(_colar(b, _qr(8), 600, 1400), (0, 0), 3.2)
    _jpeg(b, novo("s03_desfocada"))
    # 4. térmica desbotada e com baixo contraste
    b = _texto_falso(_papel(rng, 190, 10), rng)
    qr = _qr(8)
    furos = rng.random(qr.shape) < 0.08
    qr = qr.copy()
    qr[furos] = 255
    _jpeg(_colar(b, qr, 620, 1450, escuro=120, claro=200), novo("s04_desbotada"))
    # 5. girada e em perspectiva (cupom fotografado de lado)
    b = _texto_falso(_papel(rng), rng)
    b = _colar(b, _qr(9), 600, 1300)
    src = np.float32([[0, 0], [W, 0], [W, H], [0, H]])
    dst = np.float32([[140, 60], [W - 60, 220], [W - 220, H - 40], [40, H - 200]])
    b = cv2.warpPerspective(b, cv2.getPerspectiveTransform(src, dst), (W, H), borderValue=200)
    _jpeg(b, novo("s05_perspectiva"))
    # 6. sombra forte e reflexo (iluminação irregular)
    b = _texto_falso(_papel(rng), rng)
    b = _colar(b, _qr(8), 620, 1400)
    gx = np.linspace(0.45, 1.15, W, dtype=np.float32)[None, :]
    b = b * gx
    cv2.circle(b, (1000, 1650), 260, 255, -1)
    b = cv2.GaussianBlur(b, (0, 0), 1.2)
    _jpeg(b, novo("s06_sombra_reflexo"))
    # 7. papel curvado (bobina enrolada)
    b = _texto_falso(_papel(rng), rng)
    b = _colar(b, _qr(8), 620, 1400)
    xx, yy = np.meshgrid(np.arange(W, dtype=np.float32), np.arange(H, dtype=np.float32))
    b = cv2.remap(b, xx + 40 * np.sin(np.pi * yy / H), yy + 25 * np.sin(np.pi * xx / W), cv2.INTER_LINEAR, borderValue=200)
    _jpeg(b, novo("s07_curvada"))
    # 8. ruído alto (pouca luz, ISO alto)
    b = _texto_falso(_papel(rng, 120, 30), rng)
    b = _colar(b, _qr(8), 620, 1400, escuro=40, claro=150) + rng.normal(0, 28, (H, W))
    _jpeg(b, novo("s08_ruido_escura"), qualidade=65)
    # 9. sem QR (pior caso: percorre todas as variantes)
    _jpeg(_texto_falso(_papel(rng), rng), novo("s09_sem_qr"))
    # 10. QR destruído no miolo (localizadores intactos)
    b = _texto_falso(_papel(rng), rng)
    qr = _qr(8).copy()
    m = 4 * 8 + 9 * 8
    qr[m:-m, m:-m] = np.where(rng.random(qr[m:-m, m:-m].shape) < 0.5, 0, 255)
    _jpeg(_colar(b, qr, 620, 1400), novo("s10_qr_destruido"))
    return saida


def medir_um(caminho: str, limite_ms: int) -> dict:
    """Roda NESTE processo (chamado num subprocesso novo)."""
    import resource
    import time

    from app.decoder import decode_image
    from app.imagem import validar

    rss_idle = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    dados = Path(caminho).read_bytes()
    _, w, h = validar(dados, 40_000_000)
    t0 = time.perf_counter()
    r = decode_image(dados, w, h, limite_ms=limite_ms)
    total_ms = (time.perf_counter() - t0) * 1000
    return {
        "imagem": Path(caminho).name, "largura": w, "altura": h, "bytes": len(dados),
        "limite_ms": limite_ms, "ok": r.success, "url_sp": bool(r.codes and RE_SP.match(r.codes[0])),
        "motivo": r.motivo, "estrategia": r.estrategia, "decodificador": r.decoder,
        "tentativas": r.attempts, "ms": round(total_ms, 1),
        "rss_pico_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "rss_apos_import_mb": round(rss_idle, 1),
    }


def main() -> None:
    if sys.argv[1] == "--um":
        print(json.dumps(medir_um(sys.argv[2], int(sys.argv[3]))))
        return
    fotos, saida = Path(sys.argv[1]), Path(sys.argv[2])
    limites = [int(x) for x in os.environ.get("BENCH_LIMITES", "8000,60000").split(",")]
    imagens = sorted(p for p in fotos.glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    imagens += gerar_sinteticas(saida / "sinteticas")
    resultados = []
    for img in imagens:
        for limite in limites:
            proc = subprocess.run(
                [sys.executable, "-m", "bench.benchmark", "--um", str(img), str(limite)],
                capture_output=True, text=True, timeout=180,
            )
            if proc.returncode != 0:
                linha = {"imagem": img.name, "limite_ms": limite, "erro": proc.stderr.strip().splitlines()[-1:]}
            else:
                linha = json.loads(proc.stdout.strip().splitlines()[-1])
            resultados.append(linha)
            print(json.dumps(linha, ensure_ascii=False), flush=True)
    (saida / "resultado.json").write_text(json.dumps(resultados, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
