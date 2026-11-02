"""Configuração por variáveis de ambiente (nenhuma é segredo)."""

import os


def _int(nome: str, padrao: int, minimo: int, maximo: int) -> int:
    try:
        valor = int(os.environ.get(nome, padrao))
    except ValueError:
        valor = padrao
    return max(minimo, min(maximo, valor))


# Orçamento de tempo de uma leitura (ms). O cliente pode pedir menos com X-Tempo-Limite-Ms.
TEMPO_LIMITE_MIN_MS = 200
TEMPO_LIMITE_MAX_MS = 15000
TEMPO_LIMITE_MS = _int("QR_TEMPO_LIMITE_MS", 8000, TEMPO_LIMITE_MIN_MS, TEMPO_LIMITE_MAX_MS)

# Prazo rígido = orçamento + folga. Passou disso, o processo leitor é encerrado
# (uma chamada de decodificador travada não segura o semáforo) e outro é iniciado.
FOLGA_RIGIDA_MS = _int("QR_FOLGA_RIGIDA_MS", 2000, 500, 10000)

# Leituras simultâneas. Acima disso a API responde 503 (motivo "ocupado") sem ler o corpo.
CONCORRENCIA = _int("QR_CONCORRENCIA", 1, 1, 4)

# Tamanho máximo do arquivo de imagem e folga para o envelope multipart.
MAX_BYTES = _int("QR_MAX_BYTES", 10 * 1024 * 1024, 1024, 20 * 1024 * 1024)
FOLGA_MULTIPART = 64 * 1024

# Pixels máximos declarados no cabeçalho da imagem (anti "decompression bomb").
MAX_PIXELS = _int("QR_MAX_PIXELS", 40_000_000, 1_000_000, 100_000_000)

# Lado maior acima disso: a leitura trabalha numa cópia reduzida (cada operação fica curta).
LADO_MAX = _int("QR_LADO_MAX", 2200, 800, 6000)

# Lado maior da imagem entregue ao WeChatQRCode (o decodificador mais lento).
WECHAT_LADO_MAX = _int("QR_WECHAT_LADO_MAX", 1200, 600, 2200)

DOCS = os.environ.get("QR_DOCS", "true").lower() in ("1", "true", "sim", "yes")
VERSAO = os.environ.get("QR_VERSAO", "dev")
