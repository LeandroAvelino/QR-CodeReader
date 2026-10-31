"""Instrumentação OpenTelemetry: logs e traces via OTLP.

A exportação só é ativada quando a variável de ambiente padrão
``OTEL_EXPORTER_OTLP_ENDPOINT`` está definida (ex.:
``http://otel-collector:4318``). Sem ela, a aplicação continua logando
normalmente no console — nada quebra em desenvolvimento local.
"""

import logging
import os

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup_telemetry(app: FastAPI) -> None:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    resource = Resource.create(
        {"service.name": os.environ.get("OTEL_SERVICE_NAME", "qr-reader-api")}
    )

    # --- Logs -> OTLP ---
    logger_provider = LoggerProvider(resource=resource)
    set_logger_provider(logger_provider)
    if endpoint:
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter())
        )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))

    # --- Traces -> OTLP (um span por requisição HTTP) ---
    tracer_provider = TracerProvider(resource=resource)
    if endpoint:
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)
