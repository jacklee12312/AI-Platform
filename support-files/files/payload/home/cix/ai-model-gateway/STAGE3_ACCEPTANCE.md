# Stage 3 first-version acceptance

Date: 2026-08-26  
Appliance: CIX P1, single Houmo LQ50/M50 24 GB  
Gateway: FastAPI v0.2.0 on `192.168.133.1:17700`

## Result

The first-version model cluster management and gateway layer passed functional
acceptance. Only port 17700 is reachable from the Windows host; model ports
17701-17707 and CV port 17710 are internal Docker ports.

## Model modes

| Mode | Active services | Switch result |
|---|---|---|
| `qwen38` | Qwen3.8-27B, Qwen3Guard | PASS |
| `multi-model` | CoPaw, Qwen3-VL, BGE-M3, Reranker, Qwen3Guard | PASS |

`modelctl` provides an exclusive lock, target health checks, atomic mode-state
updates and rollback to the previous mode on startup failure. A concurrent
operation was rejected with exit code 75. A systemd restart restored persisted
`qwen38` mode successfully.

## Gateway state semantics

- `online`: active and healthy
- `inactive`: deployed but belongs to the other LQ50 mode
- `error`: expected in the active mode but unhealthy
- `unavailable`: not adapted in the current delivery scope

`qwen38` mode reported 15 online, 4 inactive, 3 unavailable, 0 error.
`multi-model` mode reported 18 online, 1 inactive, 3 unavailable, 0 error.
Both modes reported overall status `ok`.

## Integration results

The automated multi-model acceptance suite passed 12 checks: health,
authentication, inactive routing, guarded chat, embeddings, rerank, the
BGE-Reranker-CoPaw RAG chain, Qwen3-VL, and four CV operations. Selected timings:

| Check | Result | Observed time |
|---|---|---:|
| Guarded CoPaw chat | PASS | 2.046 s |
| BGE-M3, 3 × 1024-dimensional vectors | PASS | 0.451 s |
| Reranker, top document index 0 | PASS | 1.223 s |
| RAG chain completion | PASS | 3.249 s |
| Qwen3-VL image description | PASS | 6.279 s |
| CV classify/detect/face/segment | PASS | 0.098-0.201 s each |
| Guarded Qwen3.8 chat | PASS | 5.506 s |

## Resilience and boundary-input results

The active `qwen38` mode also passed the resilience suite:

| Check | Result | Observed time |
|---|---|---:|
| Four concurrent guarded chats | PASS | 11.269-15.386 s per request |
| 5600-character guarded prompt | PASS | 67.813 s |
| Invalid JSON | PASS | HTTP 400 in 0.019 s |
| Damaged JPEG payload | PASS | HTTP 502 in 0.199 s |

The first long-input run found that Qwen3Guard's 8192-token context pool was
split between two parallel slots, leaving 4096 tokens per request. The Guard
pool is now 16384 tokens, providing 8192 tokens per request while retaining
two-way Guard concurrency. Qwen3.8 itself continues to run with 131072 tokens.

## Failure and recovery

- Stopping BGE-M3 changed gateway readiness to `degraded`, marked BGE as
  `error`, and returned HTTP 503 for embeddings.
- Re-running `modelctl switch multi-model` restored every expected service and
  gateway readiness.
- Stopping Qwen3Guard caused guarded chat to fail closed with HTTP 503.
- Qwen3Guard occasionally concatenated `Safety` and `Categories` labels. The
  parser now accepts this form and the 11-test unit suite includes a regression.

## Stability test

A 20-second preflight completed three guarded chat/health checks with zero
failures. The 8-hour run is active as `houmo-ai-soak-8h.service`; JSONL and final
summary files are written to `/home/cix/houmo-docker-deployment/state/`. At the
latest acceptance checkpoint it had completed 34 iterations with zero failures.
