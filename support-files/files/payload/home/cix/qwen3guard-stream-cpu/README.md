# Qwen3Guard-Stream-0.6B CPU service

This service runs the official Qwen3Guard-Stream model on ARM64 CPU and exposes:

- `GET /health`
- `POST /v1/chat/completions` for complete prompt/response moderation
- `POST /v1/moderations`
- `WS /v1/moderations/stream` for stateful token-level output moderation

The streaming path uses the model KV cache directly. The gateway holds each upstream
SSE content delta until the Guard classifies every re-tokenized Qwen3 token in that
delta, so unsafe content in the current delta is not released to the client.
