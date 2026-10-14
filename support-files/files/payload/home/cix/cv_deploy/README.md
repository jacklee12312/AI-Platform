# CIX P1 CV deployment

This bundle runs the complete `.cix` artifacts requested in the adaptation
sheet through a single NOE service.  One model is held in NPU memory at a time;
switching `model_id` unloads the previous model before loading the next one.

## Internal API

- `GET /health`
- `GET /v1/cv/models`
- `POST /v1/cv/infer/{model_id}?confidence=0.25&iou=0.45`

The POST body is either raw JPEG/PNG bytes or JSON containing
`{"image_base64":"..."}`.

The CV container is internal-only in production. Business clients use the
authenticated gateway on port 17700:

```powershell
$headers = @{ Authorization = "Bearer <API_KEY>" }
Invoke-RestMethod -Method Post `
  -Uri "http://192.168.133.1:17700/v1/cv/detect?model=yolov11_n&confidence=0.25" `
  -Headers $headers -ContentType "image/jpeg" -InFile ".\test.jpg"
```

`compose.debug.yaml` may be added explicitly for temporary direct access during
maintenance. It is not used by the production service.

The `yolov8l_seg` endpoint currently returns detections plus mask-prototype
metadata.  Full mask reconstruction remains CPU post-processing work.  The
ByteTrack endpoint exposes its NPU detector for single images; stable track IDs
require the sequential-video endpoint planned for the next iteration.

SAM2.1 Tiny is not included in this complete-model service because only its
encoder is a CIX NPU binary; its ONNX decoder and prompt pipeline are deployed
as a separate hybrid service.
