# Matcha 中文 TTS（CIX CPU）

该服务使用 `sherpa-onnx` 的 `matcha-icefall-zh-baker` 中文女声声学模型和
`vocos-22khz-univ` 声码器，提供与 OpenAI Speech API 相近的 HTTP 接口。

## 启动

```bash
docker compose up -d --build
curl http://192.168.133.1:17723/health
```

## 合成

```bash
curl -X POST http://192.168.133.1:17723/v1/audio/speech \
  -H 'Content-Type: application/json' \
  --data-binary @sample_request.json \
  -o matcha-zh.wav
```

响应头包含合成耗时、音频时长、RTF、第一个回调时延和回调分片数。当前
`sherpa-onnx 1.13.x` 的句子回调返回 `1` 表示继续、返回 `0` 会提前终止；服务已按
盒子上的实际运行时行为处理。
