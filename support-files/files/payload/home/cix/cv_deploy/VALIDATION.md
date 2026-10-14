# CIX P1 CV deployment validation

Date: 2026-08-26  
Target: Debian ARM64 box at `192.168.133.1`  
Runtime: CIX NOE 2.0.0, `/dev/aipu`, Docker image `cix-cv-noe:1.0.0`  
Service: `cix-cv`, internal TCP `17710`, restart policy `unless-stopped`.
The host does not publish 17710; external validation uses gateway port 17700.

All rows below passed model load and a real NPU forward from a Windows client.
Times are one functional test run, not a formal benchmark or QPS result.

| Model ID | Task | Load ms | First forward ms | Repeated forward ms | Status |
|---|---|---:|---:|---:|---|
| mobilenet_v2 | classification | 6.08 | 4.12 | 4.64 | passed |
| inception_v3 | classification | 13.85 | 6.65 | 7.48 | passed |
| yolov11_l | detection | 22.68 | 53.08 | 60.67 | passed |
| yolov11_n | detection | 4.06 | 35.45 | 35.20 | passed |
| yolov11n_obb | oriented detection | 5.28 | 23.76 | 23.73 | passed |
| yolov12_s | detection | 9.71 | 53.65 | 57.01 | passed |
| yolov12_m | detection | 12.98 | 84.06 | 89.67 | passed |
| retinaface | face detection | 1.57 | 16.96 | 17.68 | passed |
| yolov5s_face | face detection | 8.71 | 18.41 | 20.49 | passed |
| scrfd | face detection | 5.53 | 17.46 | 17.78 | passed |
| arcface | face embedding | 27.76 | 7.84 | 8.18 | passed |
| yolov8l_seg | instance segmentation | 45.75 | 67.74 | 79.01 | passed |
| bytetrack_s | tracking detector | 6.51 | 53.48 | 54.56 | passed |

## Acceptance boundaries

- Classification, standard object detection, OBB metadata, face boxes and
  landmarks, and ArcFace embeddings have structured JSON output.
- YOLOv8 segmentation currently returns boxes and confirms the NPU mask-proto
  output; full mask reconstruction is still CPU post-processing work.
- ByteTrack currently exposes the NPU detector per frame. Persistent track IDs
  require the sequential video/session API.
- SCRFD and ArcFace are available as two model endpoints. Face alignment and
  gallery matching are the remaining composite-pipeline work.
- YOLO11-M is not deployed because the requested official collection does not
  contain a corresponding `.cix` artifact.
- SAM2.1 Tiny is handled separately because only the encoder is a `.cix` NPU
  artifact and the decoder remains ONNX/CPU.
