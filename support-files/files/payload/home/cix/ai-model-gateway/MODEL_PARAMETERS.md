# Supported model parameters

Validated baseline for the 2026-08-26 first-version delivery.

| Model | Mode/device | Quantization/context | Concurrency/batch | Gateway endpoint |
|---|---|---|---|---|
| Qwen3.8-27B MTP | `qwen38`, LQ50 | W4A8, 单卡 131072 / 双卡 262144 context | `-np 1`, batch 1 | `/v1/chat/completions` |
| CoPaw-Flash-9B | `copaw`, LQ50 | W4A8, model-native 128K | `-np 2` logical slots, compiled batch 1 | `/v1/chat/completions` |
| Spark X1-4B | `spark-x1`, LQ50 | W4A8, 262144 runtime context | `-np 2`, compiled batch 1 | `/v1/chat/completions` |
| Qwen3-VL-8B | `qwen-vl`, LQ50 | model-native quantization, 32768 context | `-np 2` | `/v1/chat/completions` |
| GLM-OCR-0.9B | `glm-ocr`, LQ50 | vendor GGUF + mmproj, 8192 context | `-np 2`, batch 1 | `/v1/chat/completions`, `/v1/ocr/mineru` |
| BGE-M3 | `bge-m3`, LQ50 | adapted 0.5K input | `-np 10`, `-b/-ub 8096` | `/v1/embeddings` |
| BGE-Reranker-V2-M3 | `reranker`, LQ50 | adapted 0.5K input | `-np 10` | `/v1/rerank` |
| Qwen3Guard-Stream-0.6B | all modes, CPU | BF16, 8192 tokens per stream | 8 CPU threads, 2 stream slots | `/v1/moderations`, guarded streaming chat |
| CIX CV models | always on, CIX NOE | compiled `.cix` | one model resident at a time | `/v1/cv/*` |

Gateway defaults and limits:

- Guard mode: `block`, fail closed
- Rate limit: 120 requests/minute per client
- Chat timeout: 300 seconds
- Guard timeout: 90 seconds
- CV timeout: 180 seconds
- Maximum request body: 25 MiB
- CV confidence default: 0.25
- CV IoU default: 0.45

Application callers should pass task-specific `temperature`, `top_p`, and
`max_tokens`; judicial-scene quality tuning remains a later business iteration.

## Qwen3.8 27B 多模态（2026-09-11）

单卡使用 128K `1d-xh2` 主模型，双卡使用 256K `2d-xh2` 主模型；两者共用动态视觉投影 GGUF 和 HLIELLama 2.2.0 运行时。通过 OpenAI 兼容 `messages[].content` 中的 `image_url` 提交图片。两种卡数的安装验收均包含真实图片识别和文本推理。
