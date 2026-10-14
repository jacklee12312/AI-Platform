# Gateway v0.2 validation

Validated on the CIX P1 / LQ50 appliance on 2026-08-26.

| Check | Result |
|---|---|
| Container health | PASS |
| Missing API key returns OpenAI-style 401 | PASS |
| `/v1/models` reports online/inactive/error/unavailable | PASS |
| Qwen3Guard moderation: safe input | PASS |
| Qwen3Guard moderation: unsafe input | PASS |
| Guard blocks unsafe chat input | PASS |
| Safe Qwen3-VL chat gets input and output review | PASS |
| Qwen3.8-27B direct HLIELlama chat | PASS (LQ50, 128K context) |
| Qwen3.8-27B gateway chat with input/output Guard | PASS |
| BGE-M3 embeddings through gateway | PASS (2 vectors, 1024 dimensions) |
| BGE-Reranker-V2-M3 through gateway | PASS |
| CV classify/detect/face/segment through gateway | PASS |
| Qwen-VL + Guard + BGE + Reranker concurrent residency | PASS |
| Windows authenticated request to `192.168.133.1:17700` | PASS |
| systemd boot service enabled and active | PASS |
| `modelctl` qwen38 → multi-model switch | PASS |
| `modelctl` multi-model → qwen38 switch | PASS |
| Concurrent mode-switch lock | PASS (second operation exits 75) |
| Persisted mode restored by systemd restart | PASS |
| Inactive models do not degrade active mode | PASS |
| Expected model failure reports `error` and HTTP 503 | PASS |
| Same-mode reconciliation restores a stopped model | PASS |
| Guard unavailable fails closed | PASS |
| Only gateway port 17700 reachable from Windows | PASS |
| Four concurrent guarded Qwen3.8 requests | PASS (11.269-15.386 s each) |
| 5600-character guarded input | PASS (67.813 s) |
| Invalid JSON returns structured HTTP 400 | PASS |
| Damaged JPEG returns structured upstream error | PASS |

In `qwen38` mode, `/health` reports 15 online, 4 inactive, 3 unavailable,
and 0 error models. In `multi-model` mode it reports 18 online, 1 inactive,
3 unavailable, and 0 error models. Both modes report overall status `ok`.

Observed service memory from `docker stats --no-stream`:

- Gateway: about 40 MiB (limit 512 MiB)
- Qwen3Guard: about 1.88 GiB after context-pool increase (limit 4 GiB)
- Qwen3-VL host memory: about 2.3 GiB
- BGE-M3 host memory: about 188 MiB
- Reranker host memory: about 172 MiB

The gateway reports `degraded` only when a model expected in the active mode is
in `error`. Inactive and unavailable models do not affect readiness. Liveness
is exposed separately at `/health/live` for Docker.

## Qwen3.8-27B exclusive mode

The `HiModel-Qwen3.8-27b-mtp-w4a8-p256-128K-b1-2core-1d-xh2-v1.5.0`
artifact was validated with HLIELlama 2.1.0 on the single LQ50. The runtime
reported a 131072-token context and verify-style decode graph. A direct API
request completed at about 5.58 generated tokens/s; the guarded gateway request
completed successfully with both input and output assessed as `Safe`.

This 27B model runs in the `qwen38` Compose profile and is treated as an
exclusive LQ50 workload. CoPaw, Qwen3-VL, BGE-M3, and Reranker are inactive
while it is active. Only the unified gateway is published on port 17700.

Qwen3Guard uses a 16384-token shared CPU context pool with `-np 2`, yielding
8192 tokens per guarded request. This corrects the former 4096-token effective
limit without reducing Guard concurrency.

## Multi-model integration acceptance

The automated integration test passed 12 checks through the gateway:

- health and authentication enforcement
- inactive Qwen3.8 request rejection
- guarded CoPaw chat
- three BGE-M3 vectors at 1024 dimensions
- BGE-Reranker top-document selection
- BGE → Reranker → CoPaw RAG chain
- Qwen3-VL image description
- CV classification, detection, face detection, and segmentation

An observed Qwen3Guard response concatenated `Safety` and `Categories` without a
newline. The parser was made tolerant of this valid-but-irregular form and a
regression test was added. The local unit suite now passes 11 tests.

An 8-hour `qwen38` soak was started as transient unit
`houmo-ai-soak-8h.service`; results are written under the deployment `state/`
directory. A 20-second preflight completed 3 checks with 0 failures. The latest
checkpoint contained 34 iterations and 0 failures.
