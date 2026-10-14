from __future__ import annotations

import io
import os
import threading
import time
import wave
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from kokoro import KModel, KPipeline


SAMPLE_RATE = 24000
MODEL_NAME = "kokoro-82m"
MODEL_DIR = os.environ.get("KOKORO_MODEL_DIR", "/models")
VOICE_PATH = os.path.join(MODEL_DIR, "voices", "zf_xiaoyi.pt")
_lock = threading.Lock()
_model: KModel | None = None
_pipeline: KPipeline | None = None
_loaded_at = 0.0


class SpeechRequest(BaseModel):
    model: str = MODEL_NAME
    input: str = Field(min_length=1, max_length=2000)
    voice: str = "zf_xiaoyi"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    response_format: str = "wav"


def _load() -> None:
    global _model, _pipeline, _loaded_at
    os.chdir(MODEL_DIR)
    started = time.perf_counter()
    _model = KModel(os.environ.get("KOKORO_CONFIG", os.path.join(MODEL_DIR, "datasets", "npu_config.json")), use_npu=True)
    _pipeline = KPipeline(lang_code="z", model=_model)
    _loaded_at = time.perf_counter() - started


def _wav_bytes(audio: np.ndarray) -> bytes:
    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2", copy=False)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    return output.getvalue()


def _synthesize(request: SpeechRequest) -> tuple[bytes, float, float, int]:
    if request.model not in {MODEL_NAME, "kokoro", "tts-1"}:
        raise ValueError(f"unsupported model: {request.model}")
    if request.voice != "zf_xiaoyi":
        raise ValueError("only the Mandarin voice zf_xiaoyi is installed")
    if request.response_format.lower() != "wav":
        raise ValueError("only response_format=wav is supported")
    assert _pipeline is not None
    started = time.perf_counter()
    chunks: list[np.ndarray] = []
    with _lock:
        for result in _pipeline(
            request.input,
            voice=VOICE_PATH,
            speed=request.speed,
        ):
            if result.audio is not None:
                chunks.append(np.asarray(result.audio, dtype=np.float32).reshape(-1))
    if not chunks:
        raise ValueError("the input produced no audio")
    audio = np.concatenate(chunks)
    synthesis_seconds = time.perf_counter() - started
    audio_seconds = len(audio) / SAMPLE_RATE
    return _wav_bytes(audio), synthesis_seconds, audio_seconds, len(chunks)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _load()
    yield
    if _model is not None:
        _model.clean()


app = FastAPI(title="CIX Kokoro TTS", version="0.2.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok" if _model is not None else "loading",
        "model": MODEL_NAME,
        "voice": "zf_xiaoyi",
        "sample_rate": SAMPLE_RATE,
        "backend": "CIX NPU + ONNX Runtime CPU",
        "load_seconds": round(_loaded_at, 3),
    }


@app.post("/v1/audio/speech")
async def speech(request: SpeechRequest) -> Response:
    try:
        body, synthesis_seconds, audio_seconds, chunks = await run_in_threadpool(
            _synthesize, request
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rtf = synthesis_seconds / audio_seconds if audio_seconds else 0.0
    return Response(
        content=body,
        media_type="audio/wav",
        headers={
            "X-TTS-Model": MODEL_NAME,
            "X-TTS-Synthesis-Ms": str(round(synthesis_seconds * 1000, 1)),
            "X-Audio-Duration-Ms": str(round(audio_seconds * 1000, 1)),
            "X-Real-Time-Factor": str(round(rtf, 4)),
            "X-TTS-Chunks": str(chunks),
        },
    )
