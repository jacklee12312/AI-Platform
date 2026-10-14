# Houmo M50 model services

This deployment manages HLIELlama services on the Houmo LQ50/M50 NPU. Exactly
one LQ50 model mode is active at a time:

- CoPaw-Flash-9B (`copaw`, 2 slots): internal port `17701`
- Spark X1-4B (`spark-x1`, 2 slots, 256K runtime context): internal port `17711`
- BGE-M3 embeddings (`bge-m3`, 10 slots): internal port `17702`
- BGE-Reranker-V2-M3 (`reranker`, 10 slots): internal port `17703`
- Qwen3-VL-8B (`qwen-vl`, 2 slots): internal port `17705`
- Qwen3Guard-Stream-0.6B (CPU): internal port `17706`
- Qwen3.8-27B (`qwen38`, 1 slot): internal port `17707`
- GLM-OCR-0.9B (`glm-ocr`, 2 slots): internal port `17709`

All endpoints require `Authorization: Bearer <key>`. The real key is stored only in the box-side `.env` file with mode `0600`.

## Operations

```bash
modelctl status
modelctl switch copaw
modelctl switch spark-x1
modelctl switch qwen38
modelctl switch qwen-vl
modelctl switch glm-ocr
modelctl switch bge-m3
modelctl switch reranker
```

`modelctl` serializes operations with `flock`, stops the conflicting LQ50
services, waits for every target health check, writes the active-mode file
atomically, and leaves all accelerator models stopped if target startup fails.
ModelDeck performs functional inference verification and a bounded rollback
transaction around this lower-level command.

The supported modes are `copaw`, `spark-x1`, `qwen38`, `qwen-vl`, `glm-ocr`,
`bge-m3`, and `reranker`. CPU Qwen3Guard remains independent. The selected mode is persisted in
`state/active-mode` and restored by systemd after reboot.

## Network exposure

Production Compose files do not publish model ports. Business clients use only
the authenticated gateway at `192.168.133.1:17700`; the gateway reaches model
containers over Docker DNS.

For temporary LAN debugging only, explicitly add the override:

```bash
docker compose -f compose.yaml -f compose.debug.yaml --profile qwen38 up -d qwen38
```

The model files and Houmo driver SDK are bind-mounted read-only. The image contains only the HLIELlama runtime and the `curl` health-check client.

Boot ordering is managed by `/etc/systemd/system/houmo-ai.service`. Its
`active-model.conf` drop-in runs `modelctl boot` and `modelctl stop`. The
container-level `on-failure:5` policy covers unexpected process exits.

## Acceptance and soak tests

```bash
set -a
. /home/cix/houmo-docker-deployment/.env
set +a
python3 /home/cix/ai-model-gateway/tests/integration_acceptance.py
python3 /home/cix/ai-model-gateway/tests/resilience_acceptance.py
./soak-test.sh 28800 60
```

The first command validates the current active mode. The second covers four-way
chat concurrency, long guarded input, malformed JSON and damaged images. The
third performs an 8-hour guarded chat and health soak at 60-second intervals and
stores JSONL plus a summary under `state/`.

### Four-hour long-context stability (Qwen3.8 or CoPaw)

The existing `qwen38-long-context-test.py` filename is retained for compatibility;
its stability phase now accepts either model:

```bash
python3 qwen38-long-context-test.py --model copaw --phase stability --minutes 240 --restore-mode copaw
```

This sends continuous non-streaming requests through the gateway at approximately
8K, 64K, 118K, and 118K input tokens, using unique five-needle retrieval prompts.
It reads the actual slot count and per-slot context from `/props`, refuses targets
larger than the available context, and does not change slots or gateway timeouts.
Only one long-context runner can hold the test lock at a time. Reports are
atomically checkpointed as JSON and Markdown in `benchmark-results/` after each
request. A four-hour run is not a pass if requests/recall fail, the model restarts,
the duration is shortened, or final recovery fails. The last in-flight request
may extend beyond the four-hour deadline. `--max-requests` is only for debugging.

Qwen3Guard-Stream uses BF16 on CPU with 8 compute threads, two stream slots,
and an 8192-token limit per guarded stream. Each upstream SSE content delta is
held until the Guard classifies its re-tokenized Qwen3 tokens.

## Rollback

Stop the Compose stack with `docker compose down`, then start the previously validated native `llama-server` commands. Native test logs are retained under:

- `/home/cix/cix_telecom_projects/houmo-HLIELLama-xh2/models/copaw_flash_9b_w4a8_128k_b1_1chip_2cores_v1.3.0/`
- `/home/cix/bge-residency-test/`
