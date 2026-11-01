# QR Code Reader API

Backend leve para leitura de QR Codes com **equalização adaptativa de imagem**.
A API recebe uma imagem, analisa como ela está (brilho, contraste e nitidez)
e aplica apenas as correções necessárias para tornar o código legível antes
de decodificá-lo. Uso previsto: serviço **interno** do n8n (cadastro de cupons
fiscais por foto), sem porta pública e sem acesso à internet.

## Como funciona

1. **Validação** (`app/imagem.py`): tipo real pelos bytes (só **JPEG** e
   **PNG**; TIFF, BMP, WebP e outros são recusados) e largura × altura lidas
   do cabeçalho **antes** de descomprimir (anti "decompression bomb").
2. **Carga em cinza com pré-redução** (`app/decoder.py`): foto com lado
   maior acima de `QR_LADO_MAX` (2200 px) é lida já reduzida, para que cada
   operação do pipeline seja curta.
3. **Análise** (`app/analyzer.py`): brilho médio, contraste (desvio-padrão) e
   nitidez (variância do Laplaciano); classifica a imagem como escura, clara
   demais, de baixo contraste, borrada, pequena e/ou grande.
4. **Equalização adaptativa** (`app/enhancer.py`): até 19 variantes, da mais
   barata à mais cara (original, equalizada, mediana, reduzida, Otsu,
   adaptativa 51/101, reparo morfológico, último recurso com denoise). Cada
   variante tem um nome canônico (campo `estrategia` da resposta).
5. **Decodificação**: em cada variante, pyzbar → zxing-cpp → OpenCV →
   **WeChatQRCode** (CNN com super-resolução), parando na primeira leitura.
   O WeChat recebe a imagem com lado maior de até `QR_WECHAT_LADO_MAX`
   (1200 px): acima de ~2000 px uma única chamada dele chegou a 20–80 s numa
   foto sem QR, e com 1200 px ele leu as mesmas imagens.
6. **Orçamento de tempo** (`QR_TEMPO_LIMITE_MS`, padrão 8 s, mínimo 200 ms,
   máximo 15 s; o cliente pode pedir menos com `X-Tempo-Limite-Ms`):
   - *cooperativo*: conferido antes de cada variante e de cada decodificador;
   - *rígido* (`app/leitor.py`): a leitura roda num **processo leitor**
     separado. Se ele não responder em `limite + QR_FOLGA_RIGIDA_MS` (2 s),
     é encerrado e substituído, e a resposta é `tempo_esgotado`. Se morrer
     (ex.: falta de memória), a resposta é `erro_interno` e outro sobe.
     O processo da API não carrega o OpenCV.

Os modelos CNN do WeChatQRCode (~1 MB) ficam em `models/wechat_qrcode/`
(idênticos aos de `WeChatCV/opencv_3rdparty`, branch `wechat_qrcode`).

## Endpoints

### `POST /decode`

Aceita a imagem de dois jeitos:

- `multipart/form-data`, campo `file`;
- **corpo cru** com `Content-Type: image/jpeg`, `image/png` ou
  `application/octet-stream` (o jeito mais simples a partir de um nó Code do n8n).

Cabeçalhos opcionais: `X-Request-Id` (`[A-Za-z0-9._:-]{1,64}`, devolvido no
corpo e no cabeçalho; sem ele a API gera um), `X-Origem` (`n8n-cadastro`,
`manual`, `teste`), `X-N8n-Execucao` (id numérico da execução, só para
correlação), `X-Tempo-Limite-Ms`.

```bash
curl -X POST http://qrcode-reader:8000/decode \
     -H 'Content-Type: image/jpeg' -H 'X-Request-Id: teste-1' --data-binary @cupom.jpg
```

Resposta de leitura bem-sucedida:

```json
{
  "success": true,
  "codes": ["https://www.nfce.fazenda.sp.gov.br/qrcode?p=..."],
  "motivo": null,
  "orientacao": null,
  "qr_detectado": true,
  "estrategia": "reduzida",
  "decoder": "zxing",
  "attempts": 4,
  "enhancements_applied": ["pre_reducao", "downscale"],
  "analysis": {"width": 1920, "height": 2560, "brightness": 151.2, "contrast": 48.9, "sharpness": 63.1, "diagnostics": {"...": false}},
  "suggestion": null,
  "processing_time_ms": 412.5,
  "requisicao_id": "teste-1",
  "versao": "0.2.0"
}
```

Quando não lê, `success` é `false` e vêm dois campos estáveis:

- `motivo` (técnico, enumerado) e
- `orientacao` (texto em pt-BR para mostrar ao usuário final, sempre o mesmo para cada motivo).

| `motivo` | HTTP | Significado |
|---|---|---|
| `sem_qr` | 200 | nenhum QR Code foi detectado na imagem |
| `qr_ilegivel` | 200 | um QR Code foi detectado (posição encontrada), mas não decodificou |
| `tempo_esgotado` | 200 | o orçamento de tempo acabou antes de ler |
| `imagem_invalida` | 400 | arquivo corrompido, vazio ou multipart sem o campo `file` |
| `tipo_nao_suportado` | 415 | não é JPEG nem PNG (pelos bytes, não pelo `content-type`) |
| `muito_grande` | 413 | arquivo acima de `QR_MAX_BYTES` (10 MB) |
| `pixels_demais` | 413 | largura × altura acima de `QR_MAX_PIXELS` (40 Mpx) |
| `ocupado` | 503 | já há uma leitura em andamento (`QR_CONCORRENCIA`); `Retry-After: 5` |
| `erro_interno` | 500 | falha inesperada |

Exemplo: `{"success": false, "motivo": "sem_qr", "orientacao": "Não encontrei o QR Code na foto. Envie uma nova foto com o QR Code inteiro aparecendo, de perto, com boa luz e sem reflexo.", "suggestion": "...dica pelo diagnóstico..."}`.
O texto de cada motivo está em `ORIENTACOES` (`app/resultado.py`). `suggestion` continua
trazendo a dica montada pelo diagnóstico da imagem (pode variar).

### `GET /health`, `GET /ready`, `GET /metrics`

- `/health`: vivo (`{"status":"ok","versao":...}`); responde mesmo durante uma leitura longa.
- `/ready`: 200 só quando o processo leitor está pronto e com os modelos WeChatQRCode
  carregados (`iniciando` ou `sem_modelos_wechat` com 503).
- `/metrics`: métricas Prometheus (`qrcode_requisicoes_total`, `qrcode_leitura_duracao_seconds`,
  `qrcode_estrategia_total`, `qrcode_tentativas`, `qrcode_imagem_bytes`, `qrcode_imagem_megapixels`,
  `qrcode_falhas_total`, `qrcode_leitor_reinicios_total`, `qrcode_leitor_memoria_bytes`, `qrcode_em_andamento`,
  `qrcode_build_info` e `process_*` (este só do processo da API).

`/docs` (Swagger) só existe com `QR_DOCS=true` (desligado na imagem de produção).

## Logs e traces

- Console: uma linha JSON por evento. Evento de leitura `qrcode.leitura` com
  `requisicao_id`, `origem`, `resultado`, `motivo`, `estrategia`, `decodificador`,
  `tentativas`, `duracao_ms`, `tamanho_bytes`, `largura`, `altura`, `diagnostico`.
  Recusas (400/413/415/503) geram `qrcode.recusa`.
- **Nunca** vão para o log: o conteúdo do QR Code, o nome do arquivo, CPF/CNPJ ou a imagem.
- OTLP (logs e traces): só com `OTEL_EXPORTER_OTLP_ENDPOINT` definida
  (ex.: `http://alloy-n8n:4320`); `OTEL_SERVICE_NAME` padrão `qrcode-reader`.
  Cada leitura tem um span `qrcode.decodificar`.

## Docker

```bash
docker build -t qrcode-reader:0.2.0 --build-arg VERSAO=0.2.0 .
```

O `Dockerfile` tem 3 estágios: `build` (venv com `requirements.lock`, versões e
hashes fixados), `test` (roda o pytest; a imagem final só é montada se passar) e
`runtime` (python slim + libzbar, usuário 10001, `QR_DOCS=false`,
`OPENCV_IO_MAX_IMAGE_PIXELS`). Base fixada por digest.

O `docker-compose.yml` é o de **produção**: sem portas no host, filesystem
somente leitura (`tmpfs` em `/tmp`), `cap_drop: ALL`, limites de CPU/memória/
processos e rede `qrcode_interno` com `internal: true` (sem internet),
compartilhada só com o n8n e o coletor de telemetria. Teste local:

```bash
docker run --rm -p 127.0.0.1:18080:8000 --read-only --tmpfs /tmp --cap-drop ALL \
  --memory 768m --cpus 1 qrcode-reader:0.2.0
```

## Testes e benchmark

```bash
docker build --target test -t qrcode-reader-test .        # roda o pytest
docker run --rm --cpus 1 --memory 768m -v "$PWD/samples:/fotos:ro" -v "$PWD/bench/saida:/saida" \
  qrcode-reader-test python -m bench.benchmark /fotos /saida
```

Fotos reais (cupons com dados pessoais) ficam em `samples/` e saídas em
`bench/saida/`, ambos fora do Git.

## Dependências

`requirements.txt` (faixas) → `requirements.lock` (fixado com hashes):

```bash
# versoes-producao.txt: "pacote==versão" do requirements.lock atual (+ pacote novo), temporário
uv pip compile --generate-hashes --python-version 3.13 --python-platform x86_64-manylinux_2_28 \
  -c versoes-producao.txt -o requirements.lock requirements.txt
uv pip compile --generate-hashes --python-version 3.13 --python-platform x86_64-manylinux_2_28 \
  -c versoes-producao.txt -o requirements-test.lock requirements-test.txt
```

Sem `-c`, o comando atualiza todas as versões (fazer de propósito, com os testes).
`requirements-test.lock` traz as mesmas versões mais pytest, httpx, qrcode e pillow.

## Ajuste de parâmetros

Limiares da análise em `app/analyzer.py`; limites operacionais por variável de
ambiente em `app/config.py` (`QR_TEMPO_LIMITE_MS`, `QR_CONCORRENCIA`,
`QR_FOLGA_RIGIDA_MS`, `QR_MAX_BYTES`, `QR_MAX_PIXELS`, `QR_LADO_MAX`,
`QR_WECHAT_LADO_MAX`, `QR_DOCS`, `QR_VERSAO`).
