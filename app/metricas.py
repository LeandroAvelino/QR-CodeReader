"""Métricas Prometheus expostas em /metrics (lidas por scrape; sem push).

Todos os labels têm valores de uma lista fixa (baixa cardinalidade).
Também expõe as métricas padrão do processo (process_cpu_seconds_total,
process_resident_memory_bytes, process_start_time_seconds), que são só do
processo da API; a memória do processo leitor (onde a decodificação acontece)
fica em qrcode_leitor_memoria_bytes.
"""

from prometheus_client import Counter, Gauge, Histogram

from . import config

REQUISICOES = Counter(
    "qrcode_requisicoes",
    "Requisições recebidas, por rota, classe HTTP e resultado.",
    ["rota", "classe", "resultado"],
)
DURACAO = Histogram(
    "qrcode_leitura_duracao_seconds",
    "Tempo da leitura (decodificação), por resultado.",
    ["resultado"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 12, 15, 30),
)
ESTRATEGIA = Counter(
    "qrcode_estrategia",
    "Leituras bem-sucedidas por variante da imagem e decodificador vencedores.",
    ["estrategia", "decodificador"],
)
TENTATIVAS = Histogram(
    "qrcode_tentativas",
    "Variantes tentadas por leitura.",
    buckets=(1, 2, 3, 5, 8, 12, 19),
)
IMAGEM_BYTES = Histogram(
    "qrcode_imagem_bytes",
    "Tamanho do arquivo de imagem recebido.",
    buckets=(50e3, 100e3, 250e3, 500e3, 1e6, 2e6, 4e6, 6e6, 8e6, 10e6),
)
IMAGEM_MEGAPIXELS = Histogram(
    "qrcode_imagem_megapixels",
    "Resolução da imagem recebida (megapixels).",
    buckets=(0.3, 1, 2, 3, 5, 8, 12, 20, 40),
)
FALHAS = Counter(
    "qrcode_falhas",
    "Leituras e requisições que não devolveram QR Code, por motivo técnico.",
    ["motivo"],
)
REINICIOS = Counter(
    "qrcode_leitor_reinicios",
    "Processos leitores encerrados e substituídos (prazo rígido estourado ou processo morto).",
    ["motivo"],
)
LEITOR_MEMORIA = Gauge(
    "qrcode_leitor_memoria_bytes",
    "Memória residente (RSS) somada dos processos leitores.",
)
EM_ANDAMENTO = Gauge("qrcode_em_andamento", "Leituras em andamento agora.")
BUILD = Gauge("qrcode_build_info", "Versão em execução (valor sempre 1).", ["versao"])
BUILD.labels(versao=config.VERSAO).set(1)


def classe_http(status: int) -> str:
    return f"{status // 100}xx"
