from __future__ import annotations

import io
import os
import threading
import time
import wave
from contextlib import asynccontextmanager

import numpy as np
import sherpa_onnx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool


MODEL_NAME = "matcha-zh"
VOICE_NAME = "zh-baker-female"
MODEL_ROOT = os.environ.get("MATCHA_MODEL_DIR", "/models/matcha")
ACOUSTIC_ROOT = os.path.join(MODEL_ROOT, "matcha-icefall-zh-baker")
NUM_THREADS = int(os.environ.get("MATCHA_NUM_THREADS", "4"))

_lock = threading.Lock()
_tts: sherpa_onnx.OfflineTts | None = None
_loaded_at = 0.0


class SpeechRequest(BaseModel):
    model: str = MODEL_NAME
    input: str = Field(min_length=1, max_length=2000)
    voice: str = VOICE_NAME
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    response_format: str = "wav"


def _load() -> None:
    global _tts, _loaded_at
    started = time.perf_counter()
    matcha = sherpa_onnx.OfflineTtsMatchaModelConfig(
        acoustic_model=os.path.join(ACOUSTIC_ROOT, "model-steps-3.onnx"),
        vocoder=os.path.join(MODEL_ROOT, "vocos-22khz-univ.onnx"),
        lexicon=os.path.join(ACOUSTIC_ROOT, "lexicon.txt"),
        tokens=os.path.join(ACOUSTIC_ROOT, "tokens.txt"),
        dict_dir=os.path.join(ACOUSTIC_ROOT, "dict"),
    )
    model = sherpa_onnx.OfflineTtsModelConfig(
        matcha=matcha,
        num_threads=NUM_THREADS,
        debug=False,
        provider="cpu",
    )
    config = sherpa_onnx.OfflineTtsConfig(
        model=model,
        rule_fsts=",".join(
            [
                os.path.join(ACOUSTIC_ROOT, "date.fst"),
                os.path.join(ACOUSTIC_ROOT, "number.fst"),
                os.path.join(ACOUSTIC_ROOT, "phone.fst"),
            ]
        ),
        max_num_sentences=1,
        silence_scale=0.2,
    )
    _tts = sherpa_onnx.OfflineTts(config)
    _loaded_at = time.perf_counter() - started


def _wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2", copy=False)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())
    return output.getvalue()


def _synthesize(request: SpeechRequest) -> tuple[bytes, float, float, float, int]:
    if request.model not in {MODEL_NAME, "matcha", "tts-1"}:
        raise ValueError(f"unsupported model: {request.model}")
    if request.voice not in {VOICE_NAME, "zh_female", "alloy"}:
        raise ValueError(f"only the Mandarin voice {VOICE_NAME} is installed")
    if request.response_format.lower() != "wav":
        raise ValueError("only response_format=wav is supported")
    if _tts is None:
        raise RuntimeError("model is not loaded")

    started = time.perf_counter()
    first_callback_seconds: float | None = None
    callback_chunks = 0

    def callback(samples: np.ndarray, progress: float) -> int:
        del samples, progress
        nonlocal first_callback_seconds, callback_chunks
        callback_chunks += 1
        if first_callback_seconds is None:
            first_callback_seconds = time.perf_counter() - started
        # sherpa-onnx 1.13.x uses the callback result as ``should_continue``.
        # Its Python docstring says the opposite, so returning 0 truncates the
        # output after the first sentence chunk on this runtime.
        return 1

    with _lock:
        audio = _tts.generate(
            request.input,
            sid=0,
            speed=request.speed,
            callback=callback,
        )
    synthesis_seconds = time.perf_counter() - started
    samples = np.asarray(audio.samples, dtype=np.float32).reshape(-1)
    if samples.size == 0:
        raise ValueError("the input produced no audio")
    sample_rate = int(audio.sample_rate)
    audio_seconds = samples.size / sample_rate
    return (
        _wav_bytes(samples, sample_rate),
        synthesis_seconds,
        audio_seconds,
        first_callback_seconds if first_callback_seconds is not None else synthesis_seconds,
        callback_chunks,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    _load()
    yield


app = FastAPI(title="CIX Matcha Chinese TTS", version="0.2.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok" if _tts is not None else "loading",
        "model": MODEL_NAME,
        "voice": VOICE_NAME,
        "sample_rate": int(_tts.sample_rate) if _tts is not None else 22050,
        "backend": "sherpa-onnx CPU",
        "num_threads": NUM_THREADS,
        "load_seconds": round(_loaded_at, 3),
    }


@app.post("/v1/audio/speech")
async def speech(request: SpeechRequest) -> Response:
    try:
        body, synthesis_seconds, audio_seconds, first_callback_seconds, chunks = (
            await run_in_threadpool(_synthesize, request)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    rtf = synthesis_seconds / audio_seconds if audio_seconds else 0.0
    return Response(
        content=body,
        media_type="audio/wav",
        headers={
            "X-TTS-Model": MODEL_NAME,
            "X-TTS-Synthesis-Ms": str(round(synthesis_seconds * 1000, 1)),
            "X-TTS-First-Callback-Ms": str(round(first_callback_seconds * 1000, 1)),
            "X-Audio-Duration-Ms": str(round(audio_seconds * 1000, 1)),
            "X-Real-Time-Factor": str(round(rtf, 4)),
            "X-TTS-Callback-Chunks": str(chunks),
        },
    )
