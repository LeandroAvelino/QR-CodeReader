FROM python:3.13-slim

# libzbar0: biblioteca nativa exigida pelo pyzbar
# libglib2.0-0: dependência do opencv-contrib headless
RUN apt-get update \
    && apt-get install -y --no-install-recommends libzbar0 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/qr-reader

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY models/ models/

# usuário sem privilégios
RUN useradd --create-home --shell /usr/sbin/nologin qrapi
USER qrapi

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
