"""Logs JSON no console e, se configurado, logs e traces via OTLP.

- Console (stdout): sempre, uma linha JSON por evento (``docker logs``).
- OTLP: só quando ``OTEL_EXPORTER_OTLP_ENDPOINT`` está definida
  (ex.: ``http://alloy-n8n:4320``). O nome do serviço vem de
  ``OTEL_SERVICE_NAME`` (padrão ``qrcode-reader``). A exportação roda em
  thread própria: se o coletor cair, a leitura continua (só a telemetria se perde).

Regra: nenhum log traz o conteúdo do QR Code, nome de arquivo, CPF/CNPJ ou a imagem.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from . import config

SERVICO_PADRAO = "qrcode-reader"

# Atributos padrão do LogRecord: não entram no JSON.
_PADRAO = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime", "taskName"}


class FormatoJson(logging.Formatter):
    """Uma linha JSON; atributos "qrcode.x" viram "x" (os demais ficam como estão)."""

    def format(self, record: logging.LogRecord) -> str:
        dados = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "nivel": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for chave, valor in vars(record).items():
            if chave in _PADRAO or chave.startswith("_"):
                continue
            nome = chave
            if chave == "event.name":
                nome = "evento"
            elif chave.startswith("qrcode."):
                nome = chave[len("qrcode."):].replace(".", "_")
            dados[nome] = valor
        if record.exc_info:
            dados["erro_tipo"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(dados, ensure_ascii=False, default=str)


def _configurar_console() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(FormatoJson())
    handler.set_name("console-json")
    if not any(h.get_name() == "console-json" for h in root.handlers):
        root.addHandler(handler)


def _configurar_otlp_logs(resource: Resource) -> None:
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    # O LoggingHandler do opentelemetry-sdk está obsoleto (e será removido); o atual
    # vem do opentelemetry-instrumentation-logging, que está no requirements.
    from opentelemetry.instrumentation.logging.handler import LoggingHandler

    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(provider)
    # Só os eventos da aplicação vão ao Loki (o Uvicorn fica só no console).
    logging.getLogger("qr_reader").addHandler(LoggingHandler(level=logging.INFO, logger_provider=provider))


def setup_telemetry(app: FastAPI) -> None:
    _configurar_console()
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    resource = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME", SERVICO_PADRAO),
            "service.version": config.VERSAO,
        }
    )
    tracer_provider = TracerProvider(resource=resource)
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        _configurar_otlp_logs(resource)
    trace.set_tracer_provider(tracer_provider)
    # Um span por requisição; /health, /ready e /metrics ficam de fora
    # (OTEL_PYTHON_FASTAPI_EXCLUDED_URLS no ambiente, com este padrão).
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        excluded_urls=os.environ.get("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "health,ready,metrics"),
    )
