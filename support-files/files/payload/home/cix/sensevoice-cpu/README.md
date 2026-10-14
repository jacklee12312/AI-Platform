# SenseVoice CPU service

ARM64 CPU deployment of `SenseVoiceSmall` using the int8 sherpa-onnx model.

Interfaces:

- `POST /v1/audio/transcriptions`: OpenAI-compatible multipart transcription.
- `POST /v1/audio/asr`: Alias of the transcription endpoint.
- `WS /v1/audio/transcriptions/stream`: 16 kHz mono signed 16-bit little-endian PCM with VAD-based simulated streaming.
- `GET /health`: Authenticated readiness and model metadata.

The first offline image accepts uncompressed PCM WAVE input (8/16/24/32-bit,
mono or multi-channel). MP3/M4A conversion is intentionally left to the client
until an audited ARM64 ffmpeg package is added to the offline bundle.

The streaming protocol starts with a JSON message:

```json
{"type":"start","model":"sensevoice","sample_rate":16000,"channels":1,"format":"pcm_s16le","language":"auto","use_itn":true}
```

Send binary PCM frames, then finish with `{"type":"stop"}`. The service emits a final transcript for each VAD-detected utterance and a final `done` event.

This is simulated streaming: SenseVoiceSmall itself is a non-streaming model and each completed speech segment is decoded after VAD endpoint detection.

## Offline deployment

The ARM64 runtime image is stored at
`docker-offline/images/sensevoice-cpu-0.1.0-arm64-image.tar.gz`. Extract it and
load the resulting tar with `docker load -i`. The source bundle
`sensevoice-cpu-v0.1.0-arm64.tar.gz` contains the compose file, service code,
offline wheels, model, VAD, tokens, and test audio.

The compose file expects the model files at:

```text
/home/cix/cix_telecom_projects/sensevoice/model/model.int8.onnx
/home/cix/cix_telecom_projects/sensevoice/model/tokens.txt
/home/cix/cix_telecom_projects/sensevoice/model/silero_vad.onnx
```

It also reads `HOUMO_API_KEY` from
`/home/cix/houmo-docker-deployment/.env` and joins the external Docker network
`houmo-ai_default`.
