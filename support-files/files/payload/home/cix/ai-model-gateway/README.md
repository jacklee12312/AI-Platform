# AI Model Gateway

FastAPI gateway for the ARM64 CIX/Houmo model appliance. It exposes a managed
set of API keys on a single port while routing requests to the existing model containers.

Key model permissions, expiry, rate/concurrency limits, daily request quotas and monthly token warnings are managed through ModelDeck. Global/model/device-pool admission limits are persisted alongside the usage ledger. See [Key 权限与资源控制](../docs/manuals/ModelDeck_Key权限与资源控制.md) for defaults, API semantics and scope.

## Public endpoints

- `GET /health` and `GET /health/live`
- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/embeddings`
- `POST /v1/rerank`
- `POST /v1/moderations`
- `POST /v1/cv/classify`
- `POST /v1/cv/detect`
- `POST /v1/cv/face`
- `POST /v1/cv/segment`
- ASR: `/v1/audio/transcriptions`, `/v1/audio/asr`, `WS /v1/audio/transcriptions/stream`
- GLM-OCR: OpenAI-compatible multimodal requests on `/v1/chat/completions`
- MinerU compatibility alias: `/v1/ocr/mineru` (non-streaming JSON, served by GLM-OCR)
- Text to speech: `/v1/audio/speech` and `/v1/audio/tts` (choose `kokoro-82m` or CPU-based `matcha-zh` in the request's `model` field)

All `/v1` routes require `Authorization: Bearer <key>`. On first startup the
legacy `HOUMO_API_KEY` from `/home/cix/houmo-docker-deployment/.env` is imported
as the default compatibility Key. Administrators can then create, disable and
revoke additional Keys in ModelDeck. Only SHA-256 digests are stored; generated
plaintext Keys are shown once.

## API keys and usage accounting

The writable `/app/data/gateway-usage.db` SQLite database contains the Key
registry and per-request token ledger. Chat requests (including SSE streams),
embeddings and rerank requests record `input_tokens`, `output_tokens` and
`total_tokens` when supplied by the upstream model. Streaming chat requests
automatically request the final OpenAI-compatible usage chunk. Calls from the
ModelDeck test console use a separate management token and are not charged to
a customer Key.

Internal Key-management and usage-query endpoints require the independent
`X-Gateway-Admin-Token` header. This token is generated during installation and
is never exposed in the browser or accepted as a customer API Key.

### S08 reconciliation semantics (2026-09-04)

The ledger records chat, embeddings, rerank and the legacy OCR alias. It is
not an all-modal billing system: audio/CV duration or costs are not converted
into text tokens. Authenticated rejected/failed requests are also recorded;
requests without a valid business Key cannot be attributed to a customer.

- Missing counters are returned as `null`, not measured zero. Aggregates are
  **known subtotals**, with `missing_*_tokens_requests` and anomaly counts.
- `usage_quality` distinguishes `reported`, `derived`, `partial`, `missing`,
  `invalid`, `inconsistent`, and `legacy`. Request success is a separate field.
- Total is derived from input + output only when the total field is absent
  and both operands are known. A conflicting upstream total is retained and
  flagged. Invalid counters are marked, not silently cast to zero.
- Embeddings/rerank do not generate text: an absent output counter is derived
  as zero and explicitly sourced. This rule does not apply to chat/OCR.
- SSE stores one terminal record after completion/disconnect. Before the
  upstream reports usage, consumed tokens remain unknown. This implementation
  does not guarantee lossless metering across process kill/power failure.
- Internal event IDs deduplicate repeated finalization; client Request IDs
  are correlation labels, not deduplication keys.
- Additive SQLite migration preserves existing keys/counts and labels old
  rows `legacy`; missing historical raw usage cannot be reconstructed.

`GET /internal/usage` accepts `api_key_id`, timezone-aware ISO `start`/`end`
(inclusive), exact `model`, `request_id`, `status`, `limit` and `offset`.
Summaries and details share one SQLite read snapshot; summaries cover the
whole filter, not just the current page. Daily buckets use UTC. ModelDeck's
date picker converts the operator's local day boundaries into UTC timestamps.

See [the operator supplement](../docs/manuals/ModelDeck_操作与用量核对补充.md)
and [S07/S08 validation report](../docs/reports/S07_S08_UI与用量核对_2026-09-04.md).

## Safety behavior

When the global Guard switch is enabled, chat requests for guarded models are checked before inference. Non-streaming
responses receive a complete output review. For `stream=true`, the gateway
holds each upstream SSE content delta until Qwen3Guard-Stream has classified
all Qwen3 tokens in that delta. The default `GUARD_MODE=off` bypasses Guard so
long prompts do not delay model inference. Setting `GUARD_MODE=block` blocks `Unsafe`,
allows `Safe`, and permits `Controversial` while recording it in the JSON log.

Before processing chat history, the gateway normalizes assistant messages that
contain `tool_calls` by setting their internal `content` to `null`. The input
Guard, model upstream and output Guard use the same normalized history. This
keeps OpenAI clients compatible with HLIELlama 2.1.0 multi-step tool retries
without mutating the client request or response.

## Active model modes

The gateway reads `/app/state/active-mode`, which is written atomically by
`modelctl`. Models outside the active LQ50 mode are reported as `inactive`, not
as failed. Supported status values are:

- `online`: expected in the active mode and healthy
- `inactive`: deployed but intentionally stopped in the other LQ50 mode
- `error`: expected in the active mode but its health probe failed
- `unavailable`: registered but not adapted in the current delivery scope

`/health` is `ok` when there are no `error` models. Inactive and unavailable
models do not degrade the active service set. The LQ50 modes are `copaw`,
`spark-x1`, `qwen38`, `qwen-vl`, `glm-ocr`, `bge-m3`, and `reranker`;
`modelctl` stops every conflicting accelerator container before starting and
validating the single selected model.

`GUARD_MODE` supports `block`, `audit`, and `off`. The shipped configuration uses
`off`; ModelDeck's global Guard switch changes it between `off` and `block` and
recreates the gateway. `GUARD_FAIL_CLOSED=true`
prevents unreviewed chat traffic when the guard service is unavailable.

## Runtime defaults

ModelDeck can safely update gateway request limits, timeouts, CV thresholds and
optional chat generation defaults. `GENERATION_DEFAULTS_ENABLED=false` is the
shipped behavior. When enabled, `DEFAULT_MAX_TOKENS`, `DEFAULT_TEMPERATURE` and
`DEFAULT_TOP_P` are inserted only when the caller omitted the corresponding
field; explicit API request values always take precedence. Successful chat
responses include `X-Generation-Defaults` so operators can verify which fields
were supplied by the gateway.

## CV request format

Send raw JPEG/PNG bytes or the existing JSON base64 shape accepted by `cix-cv`.

```bash
curl -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: image/jpeg" \
  --data-binary @test.jpg \
  "http://192.168.133.1:17700/v1/cv/detect?model=yolov11_n&confidence=0.25"
```

## Offline build

The `wheels/` directory contains Linux ARM64 Python wheels. On the box:

```bash
docker compose build --pull=false gateway
docker compose up -d gateway
```

The registry and active-mode directory are bind-mounted read-only, while the
usage database is stored in a dedicated writable bind mount. Changing mode does
not require rebuilding the gateway image.

The gateway joins `houmo-ai_default` and `cv_deploy_default`; it reaches model
containers by Docker DNS and does not depend on their published host ports.

## Boot service

Install `systemd/ai-model-gateway.service` as
`/etc/systemd/system/ai-model-gateway.service`, reload systemd, and enable it.
The unit starts after `network-online.target`; Docker publishes port 17700 on
`0.0.0.0`, so the authenticated gateway is reachable through any configured
wired or Wi-Fi IPv4 address.
