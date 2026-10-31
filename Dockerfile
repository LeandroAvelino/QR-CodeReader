# syntax=docker/dockerfile:1
# Imagem de produção do leitor de QR Code, em 3 estágios:
#   build   -> venv com as dependências fixadas por hash (requirements.lock)
#   test    -> roda o pytest (a imagem final só é montada se os testes passarem)
#   runtime -> python slim + libzbar, usuário 10001 sem privilégios, código somente leitura
#
#   docker build -t qrcode-reader:<versao> --build-arg VERSAO=<versao> .
#   docker build --target test -t qrcode-reader-test .     (só testes / benchmark)

# Base fixada por digest (python:3.13-slim em 31/10/2026). Atualizar de propósito, nunca implicitamente.
ARG BASE=python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb

FROM ${BASE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
RUN python -m venv /opt/venv
COPY requirements.lock /tmp/requirements.lock
RUN /opt/venv/bin/pip install --require-hashes --no-deps -r /tmp/requirements.lock

FROM ${BASE} AS sistema
# libzbar0: biblioteca nativa do pyzbar; libglib2.0-0: dependência do OpenCV contrib headless
RUN apt-get update \
 && apt-get install -y --no-install-recommends libzbar0 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 10001 qrapi \
 && useradd -u 10001 -g 10001 -M -d /nonexistent -s /usr/sbin/nologin qrapi

FROM sistema AS test
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore \
    PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS=1
COPY --from=build /opt/venv /opt/venv
COPY requirements-test.lock /tmp/requirements-test.lock
RUN pip install --require-hashes --no-deps -r /tmp/requirements-test.lock
WORKDIR /srv/qr-reader
COPY app/ app/
COPY models/ models/
COPY tests/ tests/
COPY bench/ bench/
RUN python -m pytest -q -p no:cacheprovider tests && touch /tmp/testes-ok

FROM build AS venv-final
# pip não é necessário em produção (menos 13 MB e menos ferramentas dentro do contêiner)
RUN /opt/venv/bin/python -m pip uninstall -y -q pip

FROM sistema AS runtime
COPY --from=venv-final /opt/venv /opt/venv
# Força o estágio de testes: sem /tmp/testes-ok a imagem final não é montada.
COPY --from=test /tmp/testes-ok /opt/testes-ok
WORKDIR /srv/qr-reader
COPY app/ app/
COPY models/ models/
ARG VERSAO=dev
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OPENCV_IO_MAX_IMAGE_PIXELS=40000000 OMP_NUM_THREADS=1 \
    QR_WECHAT_MODELS=/srv/qr-reader/models/wechat_qrcode \
    QR_VERSAO=${VERSAO} QR_DOCS=false
# Código e modelos pertencem ao root (o processo, como 10001, não consegue alterá-los).
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--limit-concurrency", "16", "--timeout-keep-alive", "5", "--no-server-header", "--no-access-log"]
