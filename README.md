# QR Code Reader API

Backend leve para leitura de QR Codes com **equalização adaptativa de imagem**.
A API recebe uma imagem, analisa como ela está (brilho, contraste e nitidez)
e aplica apenas as correções necessárias para tornar o código legível antes
de decodificá-lo.

## Como funciona

1. **Análise** (`app/analyzer.py`): mede brilho médio, contraste
   (desvio-padrão) e nitidez (variância do Laplaciano) e classifica a imagem
   como escura, clara demais, de baixo contraste, borrada e/ou pequena.
2. **Equalização adaptativa** (`app/enhancer.py`): gera variantes
   progressivamente mais tratadas, dirigidas pela análise:
   - correção de gamma (clarear/escurecer);
   - CLAHE (equalização de histograma adaptativa) para contraste;
   - unsharp mask para nitidez;
   - upscale para imagens pequenas e downscale para fotos grandes (também
     melhora a leitura de papel curvado, como cupons de bobina enrolados);
   - filtro de mediana, que remove o pontilhado de impressões em meio-tom;
   - binarização de Otsu e adaptativa (blocos 51 e 101);
   - reparo morfológico (erosão leve) após a binarização, que reconecta
     módulos falhados de impressões térmicas desbotadas;
   - último recurso: remoção de ruído + CLAHE forte + Otsu + reparo.
3. **Decodificação** (`app/decoder.py`): tenta ler cada variante com uma
   cadeia de quatro decodificadores, do mais rápido ao mais pesado —
   **pyzbar**, **zxing-cpp**, detector do **OpenCV** e **WeChatQRCode**
   (CNN com super-resolução, o mais eficaz para códigos densos, impressos
   em meio-tom ou danificados) — parando na primeira leitura bem-sucedida.
   A imagem original é sempre a primeira tentativa, então imagens boas
   respondem em milissegundos.

Os modelos CNN do WeChatQRCode (~1 MB) ficam em `models/wechat_qrcode/`
(baixados do repositório oficial `opencv/opencv_zoo` /
`WeChatCV/opencv_3rdparty`). Se os arquivos não existirem, a API continua
funcionando com os demais decodificadores. O caminho pode ser alterado pela
variável de ambiente `QR_WECHAT_MODELS`.

## Instalação

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## Executando

```powershell
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Documentação interativa (Swagger): http://localhost:8000/docs

### Docker

```bash
docker build -t qr-reader-api .
docker run -d --name qr-reader -p 8000:8000 qr-reader-api
```

A imagem (python:3.13-slim + libzbar0 + libglib2.0) roda como usuário sem
privilégios e tem `HEALTHCHECK` apontando para `/health`.

## Observabilidade (OpenTelemetry + Grafana)

A aplicação exporta **logs e traces** via OTLP (`app/telemetry.py`). A
exportação só é ativada quando `OTEL_EXPORTER_OTLP_ENDPOINT` está definida
(ex.: `http://otel-collector:4318`); sem ela, os logs continuam indo para o
console normalmente. O nome do serviço vem de `OTEL_SERVICE_NAME` (padrão
`qr-reader-api`).

Cada decodificação gera um log estruturado com os atributos `qr.success`,
`qr.attempts`, `qr.decoder`, `qr.enhancements`, `qr.processing_time_ms` e
`qr.filename` — úteis para dashboards de taxa de sucesso e latência.

A stack completa para leitura no Grafana sobe com:

```bash
docker compose up -d --build
```

Fluxo: **API → OTel Collector (OTLP 4318) → Loki → Grafana**.
O Grafana fica em http://localhost:3000 (admin/admin) com o datasource Loki
já provisionado. Para ver os logs: Explore → Loki → consulta
`{service_name="qr-reader-api"}`.

## Endpoints

### `POST /decode`

Recebe a imagem como `multipart/form-data` (campo `file`). Formatos aceitos:
JPEG, PNG, WebP, BMP, TIFF. Limite: 10 MB.

```bash
curl -X POST http://localhost:8000/decode -F "file=@qrcode.jpg"
```

Resposta:

```json
{
  "success": true,
  "codes": ["https://exemplo.com/pedido/12345"],
  "analysis": {
    "brightness": 54.3,
    "contrast": 31.2,
    "sharpness": 88.7,
    "width": 450,
    "height": 450,
    "diagnostics": {
      "dark": true,
      "bright": false,
      "low_contrast": true,
      "blurry": true,
      "small": false
    }
  },
  "enhancements_applied": ["gamma_brighten", "clahe", "unsharp_mask"],
  "attempts": 2,
  "decoder": "pyzbar",
  "suggestion": null,
  "processing_time_ms": 41.5
}
```

- `success: false` com HTTP 200 significa que a imagem é válida mas nenhum
  QR Code foi encontrado, mesmo após todas as tentativas de equalização.
  Nesse caso `suggestion` traz uma dica de recaptura em português, montada
  a partir do diagnóstico (ex.: "fotografe em um ambiente mais iluminado";
  se as métricas globais estão boas, orienta aproximar a câmera, esticar o
  papel e fotografar de frente). Em leituras bem-sucedidas, `suggestion`
  é `null`.
- HTTP 400/413/415 indicam problemas com o arquivo enviado.

### `GET /health`

Verificação de disponibilidade: `{"status": "ok"}`.

## Testes

```powershell
.venv\Scripts\python -m pip install qrcode pillow httpx pytest
.venv\Scripts\python -m pytest tests -v
```

Os testes geram QR Codes e aplicam degradações (escuridão, excesso de
brilho, baixo contraste, desfoque, redução de tamanho e ruído) para
verificar que o pipeline recupera a legibilidade.

## Ajuste de parâmetros

Os limiares que classificam a imagem ficam em `app/analyzer.py`
(`BRIGHTNESS_LOW`, `BRIGHTNESS_HIGH`, `CONTRAST_LOW`, `SHARPNESS_LOW`,
`MIN_DIMENSION`). Ajuste-os conforme o perfil das imagens recebidas em
produção. O campo `enhancements_applied` da resposta mostra o que foi
necessário em cada leitura — útil para calibrar esses valores ou, no
futuro, treinar um modelo com os casos reais.
