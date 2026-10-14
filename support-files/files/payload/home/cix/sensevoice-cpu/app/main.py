from __future__ import annotations

import asyncio
import io
import json
import os
import secrets
import threading
import time
import wave
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import sherpa_onnx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse


VERSION = "0.1.0"
MODEL_ID = "sensevoice"
MODEL_PATH = Path(os.getenv("SENSEVOICE_MODEL", "/models/model.int8.onnx"))
TOKENS_PATH = Path(os.getenv("SENSEVOICE_TOKENS", "/models/tokens.txt"))
VAD_PATH = Path(os.getenv("SENSEVOICE_VAD", "/models/silero_vad.onnx"))
API_KEY = os.getenv("HOUMO_API_KEY", "")
NUM_THREADS = max(1, int(os.getenv("SENSEVOICE_NUM_THREADS", "4")))
MAX_CONCURRENCY = max(1, int(os.getenv("SENSEVOICE_MAX_CONCURRENCY", "2")))
MAX_FILE_BYTES = max(1, int(os.getenv("SENSEVOICE_MAX_FILE_BYTES", str(100 * 1024 * 1024))))
MAX_AUDIO_SECONDS = max(1.0, float(os.getenv("SENSEVOICE_MAX_AUDIO_SECONDS", "3600")))
STREAM_MAX_SECONDS = max(1.0, float(os.getenv("SENSEVOICE_STREAM_MAX_SECONDS", "3600")))
SUPPORTED_LANGUAGES = {"", "auto", "zh", "en", "ja", "ko", "yue"}


def log_event(event: str, **fields: Any) -> None:
    print(json.dumps({"ts": time.time(), "event": event, **fields}, ensure_ascii=False, separators=(",", ":")), flush=True)


def _bearer_value(value: str) -> str:
    prefix = "Bearer "
    return value[len(prefix):] if value.startswith(prefix) else ""


def _valid_key(value: str) -> bool:
    return bool(API_KEY) and secrets.compare_digest(value, API_KEY)


async def require_api_key(request: Request) -> None:
    if not _valid_key(_bearer_value(request.headers.get("Authorization", ""))):
        raise HTTPException(status_code=401, detail="Invalid or missing API key", headers={"WWW-Authenticate": "Bearer"})


def _normalize_language(language: str | None) -> str:
    value = (language or "auto").strip().lower()
    if value not in SUPPORTED_LANGUAGES:
        raise ValueError("language must be one of auto, zh, en, ja, ko, yue")
    return value or "auto"


def _resample_linear(samples: np.ndarray, source_rate: int, target_rate: int = 16000) -> np.ndarray:
    if source_rate == target_rate:
        return np.ascontiguousarray(samples, dtype=np.float32)
    if source_rate <= 0 or samples.size == 0:
        raise ValueError("invalid audio sample rate or empty audio")
    output_size = max(1, round(samples.size * target_rate / source_rate))
    source_x = np.arange(samples.size, dtype=np.float64)
    target_x = np.linspace(0, samples.size - 1, output_size, dtype=np.float64)
    return np.interp(target_x, source_x, samples).astype(np.float32)


def _read_pcm_wave(raw: bytes) -> tuple[np.ndarray, int]:
    try:
        with wave.open(io.BytesIO(raw), "rb") as handle:
            if handle.getcomptype() != "NONE":
                raise ValueError("compressed WAVE is not supported")
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frame_count = handle.getnframes()
            payload = handle.readframes(frame_count)
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"invalid WAVE file: {exc}") from exc
    if channels < 1 or sample_rate <= 0 or frame_count < 1:
        raise ValueError("WAVE file contains no audio samples")
    if sample_width == 1:
        samples = (np.frombuffer(payload, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 2:
        samples = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 3:
        packed = np.frombuffer(payload, dtype=np.uint8).reshape(-1, 3)
        values = packed[:, 0].astype(np.int32) | (packed[:, 1].astype(np.int32) << 8) | (packed[:, 2].astype(np.int32) << 16)
        values = np.where(values & 0x800000, values - 0x1000000, values)
        samples = values.astype(np.float32) / 8388608.0
    elif sample_width == 4:
        samples = np.frombuffer(payload, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported WAVE sample width: {sample_width * 8} bit")
    samples = samples.reshape(-1, channels)
    return np.mean(samples, axis=1, dtype=np.float32), sample_rate


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str
    language: str
    emotion: str
    event: str
    tokens: list[str]
    timestamps: list[float]


class RecognitionEngine:
    def __init__(self) -> None:
        for path in (MODEL_PATH, TOKENS_PATH, VAD_PATH):
            if not path.is_file():
                raise RuntimeError(f"required model file is missing: {path}")
        started = time.perf_counter()
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(MODEL_PATH),
            tokens=str(TOKENS_PATH),
            num_threads=NUM_THREADS,
            language="auto",
            use_itn=True,
            provider="cpu",
            debug=False,
        )
        self.decode_lock = threading.Lock()
        self.load_seconds = time.perf_counter() - started
        log_event("model_loaded", model=MODEL_ID, seconds=round(self.load_seconds, 3), threads=NUM_THREADS)

    def new_vad(self) -> tuple[sherpa_onnx.VoiceActivityDetector, int]:
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(VAD_PATH)
        config.silero_vad.threshold = 0.20
        config.silero_vad.min_silence_duration = 0.25
        config.silero_vad.min_speech_duration = 0.20
        config.silero_vad.max_speech_duration = 30.0
        config.sample_rate = 16000
        return sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=120), config.silero_vad.window_size

    def decode(self, samples: np.ndarray, language: str, use_itn: bool, start: float, end: float) -> TranscriptSegment:
        with self.decode_lock:
            stream = self.recognizer.create_stream()
            if hasattr(stream, "set_option"):
                stream.set_option("language", language)
                stream.set_option("use_itn", "1" if use_itn else "0")
            stream.accept_waveform(16000, np.ascontiguousarray(samples, dtype=np.float32))
            self.recognizer.decode_stream(stream)
            result = stream.result
        return TranscriptSegment(
            start=round(start, 3),
            end=round(end, 3),
            text=(result.text or "").strip(),
            language=getattr(result, "lang", "") or language,
            emotion=getattr(result, "emotion", "") or "",
            event=getattr(result, "event", "") or "",
            tokens=list(getattr(result, "tokens", []) or []),
            timestamps=[round(float(value), 3) for value in (getattr(result, "timestamps", []) or [])],
        )

    def transcribe(self, samples: np.ndarray, sample_rate: int, language: str, use_itn: bool) -> tuple[list[TranscriptSegment], float]:
        mono_16k = _resample_linear(samples, sample_rate)
        duration = mono_16k.size / 16000
        if duration > MAX_AUDIO_SECONDS:
            raise ValueError(f"audio duration exceeds {MAX_AUDIO_SECONDS:g} seconds")

        if duration <= 30.0:
            raw_segments = [(0, mono_16k)]
        else:
            vad, window_size = self.new_vad()
            offset = 0
            while offset + window_size <= mono_16k.size:
                vad.accept_waveform(mono_16k[offset:offset + window_size])
                offset += window_size
            if offset < mono_16k.size:
                tail = np.zeros(window_size, dtype=np.float32)
                tail[: mono_16k.size - offset] = mono_16k[offset:]
                vad.accept_waveform(tail)
            vad.flush()

            raw_segments = []
            while not vad.empty():
                raw_segments.append((int(vad.front.start), np.asarray(vad.front.samples, dtype=np.float32).copy()))
                vad.pop()
            if not raw_segments and mono_16k.size:
                raw_segments.append((0, mono_16k))

        segments = [
            self.decode(chunk, language, use_itn, start / 16000, (start + chunk.size) / 16000)
            for start, chunk in raw_segments
        ]
        return segments, duration


ENGINE: RecognitionEngine | None = None
DECODE_SLOTS = asyncio.Semaphore(MAX_CONCURRENCY)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global ENGINE
    if not API_KEY:
        raise RuntimeError("HOUMO_API_KEY is not configured")
    ENGINE = await asyncio.to_thread(RecognitionEngine)
    yield
    ENGINE = None


app = FastAPI(title="SenseVoice CPU Service", version=VERSION, docs_url="/docs", redoc_url=None, lifespan=lifespan)


def _engine() -> RecognitionEngine:
    if ENGINE is None:
        raise HTTPException(status_code=503, detail="SenseVoice is not ready")
    return ENGINE


@app.get("/health/live")
async def health_live():
    return {"ok": True, "service": "sensevoice-cpu", "version": VERSION}


@app.get("/health", dependencies=[Depends(require_api_key)])
async def health():
    engine = _engine()
    return {
        "ok": True,
        "service": "sensevoice-cpu",
        "version": VERSION,
        "model": MODEL_ID,
        "device": "CPU",
        "streaming": "vad_simulated",
        "load_seconds": round(engine.load_seconds, 3),
        "threads": NUM_THREADS,
    }


@app.get("/v1/models", dependencies=[Depends(require_api_key)])
async def models():
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}]}


@app.post("/v1/audio/transcriptions", dependencies=[Depends(require_api_key)])
@app.post("/v1/audio/asr", dependencies=[Depends(require_api_key)])
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form(MODEL_ID),
    language: str = Form("auto"),
    response_format: str = Form("json"),
    use_itn: bool = Form(True),
):
    if model not in {MODEL_ID, "SenseVoiceSmall"}:
        raise HTTPException(status_code=400, detail=f"Unknown model: {model}")
    if response_format not in {"json", "verbose_json", "text"}:
        raise HTTPException(status_code=400, detail="response_format must be json, verbose_json, or text")
    try:
        language = _normalize_language(language)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    raw = await file.read(MAX_FILE_BYTES + 1)
    if not raw:
        raise HTTPException(status_code=400, detail="audio file is empty")
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="audio file is too large")
    try:
        samples, sample_rate = await asyncio.to_thread(_read_pcm_wave, raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    started = time.perf_counter()
    try:
        async with DECODE_SLOTS:
            segments, duration = await asyncio.to_thread(_engine().transcribe, samples, sample_rate, language, use_itn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    inference_ms = (time.perf_counter() - started) * 1000
    text = "".join(item.text for item in segments).strip()
    rtf = inference_ms / 1000 / duration if duration else 0.0
    headers = {
        "X-Audio-Duration-S": f"{duration:.3f}",
        "X-Inference-Ms": f"{inference_ms:.3f}",
        "X-Real-Time-Factor": f"{rtf:.5f}",
    }
    log_event("transcription", filename=file.filename, duration_s=round(duration, 3), inference_ms=round(inference_ms, 3), rtf=round(rtf, 5))
    if response_format == "text":
        return PlainTextResponse(text, headers=headers)
    if response_format == "json":
        return JSONResponse({"text": text}, headers=headers)
    return JSONResponse(
        {
            "task": "transcribe",
            "language": next((item.language for item in segments if item.language), language),
            "duration": round(duration, 3),
            "text": text,
            "segments": [
                {
                    "id": index,
                    "start": item.start,
                    "end": item.end,
                    "text": item.text,
                    "language": item.language,
                    "emotion": item.emotion,
                    "event": item.event,
                    "tokens": item.tokens,
                    "timestamps": item.timestamps,
                }
                for index, item in enumerate(segments)
            ],
            "inference_ms": round(inference_ms, 3),
            "rtf": round(rtf, 5),
        },
        headers=headers,
    )


async def _stream_authorized(websocket: WebSocket) -> bool:
    header_key = _bearer_value(websocket.headers.get("Authorization", ""))
    query_key = websocket.query_params.get("api_key", "")
    return _valid_key(header_key) or _valid_key(query_key)


async def _decode_vad_segments(
    websocket: WebSocket,
    vad: sherpa_onnx.VoiceActivityDetector,
    language: str,
    use_itn: bool,
    segment_index: int,
) -> int:
    while not vad.empty():
        start_sample = int(vad.front.start)
        samples = np.asarray(vad.front.samples, dtype=np.float32).copy()
        vad.pop()
        started = time.perf_counter()
        async with DECODE_SLOTS:
            item = await asyncio.to_thread(
                _engine().decode,
                samples,
                language,
                use_itn,
                start_sample / 16000,
                (start_sample + samples.size) / 16000,
            )
        await websocket.send_json(
            {
                "type": "transcript",
                "segment": segment_index,
                "final": True,
                "text": item.text,
                "start": item.start,
                "end": item.end,
                "language": item.language,
                "emotion": item.emotion,
                "event": item.event,
                "inference_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        )
        segment_index += 1
    return segment_index


@app.websocket("/v1/audio/transcriptions/stream")
async def transcription_stream(websocket: WebSocket):
    if not await _stream_authorized(websocket):
        await websocket.close(code=4401, reason="Invalid or missing API key")
        return
    await websocket.accept()
    try:
        start_message = await asyncio.wait_for(websocket.receive_json(), timeout=15)
        if start_message.get("type") != "start":
            await websocket.send_json({"type": "error", "error": "first message must have type=start"})
            await websocket.close(code=4400)
            return
        if start_message.get("model", MODEL_ID) not in {MODEL_ID, "SenseVoiceSmall"}:
            raise ValueError("unknown model")
        if int(start_message.get("sample_rate", 16000)) != 16000:
            raise ValueError("streaming input must be 16 kHz PCM")
        if int(start_message.get("channels", 1)) != 1:
            raise ValueError("streaming input must be mono PCM")
        if start_message.get("format", "pcm_s16le") != "pcm_s16le":
            raise ValueError("streaming format must be pcm_s16le")
        language = _normalize_language(start_message.get("language", "auto"))
        use_itn = bool(start_message.get("use_itn", True))

        vad, window_size = _engine().new_vad()
        sample_buffer = np.empty(0, dtype=np.float32)
        byte_tail = b""
        received_samples = 0
        segment_index = 0
        await websocket.send_json({"type": "ready", "model": MODEL_ID, "sample_rate": 16000, "streaming": "vad_simulated"})

        while True:
            message = await websocket.receive()
            if message.get("bytes") is not None:
                data = byte_tail + message["bytes"]
                if len(data) % 2:
                    byte_tail, data = data[-1:], data[:-1]
                else:
                    byte_tail = b""
                if data:
                    chunk = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
                    received_samples += chunk.size
                    if received_samples / 16000 > STREAM_MAX_SECONDS:
                        raise ValueError(f"stream exceeds {STREAM_MAX_SECONDS:g} seconds")
                    sample_buffer = np.concatenate((sample_buffer, chunk))
                    while sample_buffer.size >= window_size:
                        vad.accept_waveform(sample_buffer[:window_size])
                        sample_buffer = sample_buffer[window_size:]
                    segment_index = await _decode_vad_segments(websocket, vad, language, use_itn, segment_index)
                continue

            text_message = message.get("text")
            if text_message is None:
                continue
            command = json.loads(text_message)
            if command.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if command.get("type") != "stop":
                raise ValueError("text messages after start must have type=ping or type=stop")
            if sample_buffer.size:
                tail = np.zeros(window_size, dtype=np.float32)
                tail[:sample_buffer.size] = sample_buffer
                vad.accept_waveform(tail)
            vad.flush()
            segment_index = await _decode_vad_segments(websocket, vad, language, use_itn, segment_index)
            await websocket.send_json({"type": "done", "segments": segment_index, "audio_duration": round(received_samples / 16000, 3)})
            await websocket.close(code=1000)
            return
    except WebSocketDisconnect:
        return
    except (ValueError, json.JSONDecodeError, asyncio.TimeoutError) as exc:
        try:
            await websocket.send_json({"type": "error", "error": str(exc)})
            await websocket.close(code=4400)
        except RuntimeError:
            pass
    except Exception as exc:
        log_event("stream_error", error=f"{type(exc).__name__}: {exc}")
        try:
            await websocket.send_json({"type": "error", "error": "internal streaming error"})
            await websocket.close(code=1011)
        except RuntimeError:
            pass
