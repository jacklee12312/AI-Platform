from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

import torch
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from transformers import AutoModel, AutoTokenizer
from transformers.cache_utils import DynamicCache


MODEL_ID = os.getenv("GUARD_MODEL_ID", "qwen3guard-stream-0.6b")
MODEL_PATH = os.getenv("GUARD_MODEL_PATH", "/models/Qwen3Guard-Stream-0.6B")
API_KEY = os.getenv("HOUMO_API_KEY", "")
MAX_TOKENS = int(os.getenv("GUARD_MAX_TOKENS", "8192"))
MAX_STREAMS = int(os.getenv("GUARD_MAX_STREAMS", "2"))
TORCH_THREADS = int(os.getenv("GUARD_CPU_THREADS", "8"))
DTYPE_NAME = os.getenv("GUARD_DTYPE", "bfloat16").lower()

torch.set_num_threads(TORCH_THREADS)
torch.set_num_interop_threads(1)


@dataclass(frozen=True)
class Assessment:
    severity: str
    category: str | None
    risk_probability: float
    category_probability: float

    @property
    def flagged(self) -> bool:
        return self.severity.lower() == "unsafe"

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "risk_probability": self.risk_probability,
            "category_probability": self.category_probability,
            "flagged": self.flagged,
        }

    def as_guard_text(self) -> str:
        category = self.category if self.severity != "Safe" and self.category else "None"
        return f"Safety: {self.severity}\nCategories: {category}"


@dataclass
class StreamState:
    past_key_values: Any
    token_count: int
    assessment: Assessment


class GuardEngine:
    def __init__(self) -> None:
        dtype = {
            "bfloat16": torch.bfloat16,
            "bf16": torch.bfloat16,
            "float32": torch.float32,
            "fp32": torch.float32,
        }.get(DTYPE_NAME)
        if dtype is None:
            raise RuntimeError("GUARD_DTYPE must be bfloat16 or float32")

        started = time.perf_counter()
        self.tokenizer = AutoTokenizer.from_pretrained(
            MODEL_PATH,
            trust_remote_code=True,
            local_files_only=True,
        )
        self.model = AutoModel.from_pretrained(
            MODEL_PATH,
            trust_remote_code=True,
            local_files_only=True,
            torch_dtype=dtype,
        ).eval()
        self.model.config.use_cache = True
        self.dtype = str(next(self.model.parameters()).dtype).replace("torch.", "")
        self.load_seconds = round(time.perf_counter() - started, 3)
        self.lock = threading.Lock()
        self.im_start_id = self.tokenizer.convert_tokens_to_ids("<|im_start|>")
        self.im_end_id = self.tokenizer.convert_tokens_to_ids("<|im_end|>")
        self.user_id = self.tokenizer.convert_tokens_to_ids("user")
        print(
            json.dumps(
                {
                    "event": "model_loaded",
                    "model": MODEL_ID,
                    "path": MODEL_PATH,
                    "dtype": self.dtype,
                    "threads": TORCH_THREADS,
                    "load_seconds": self.load_seconds,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    def _token_ids(self, messages: list[dict[str, str]], *, generation_prompt: bool) -> torch.Tensor:
        ids = self.tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=generation_prompt,
            enable_thinking=False,
            return_tensors="pt",
        )
        if ids.ndim == 2:
            ids = ids[0]
        if ids.numel() > MAX_TOKENS:
            raise ValueError(f"guard input exceeds {MAX_TOKENS} tokens")
        return ids.to(dtype=torch.long)

    def _last_user_end(self, ids: torch.Tensor) -> int:
        values = ids.tolist()
        starts = [
            index
            for index in range(len(values) - 1)
            if values[index : index + 2] == [self.im_start_id, self.user_id]
        ]
        if not starts:
            raise ValueError("guard stream requires a user message")
        start = starts[-1]
        try:
            return values.index(self.im_end_id, start + 2)
        except ValueError as exc:
            raise ValueError("guard stream could not locate the end of the user turn") from exc

    def _assessment(self, output: Any, role: str, token_index: int = -1) -> Assessment:
        if role == "user":
            risk_logits = output.query_risk_level_logits[0, token_index]
            category_logits = output.query_category_logits[0, token_index]
            risk_map = self.model.query_risk_level_map
            category_map = self.model.query_category_map
        elif role == "assistant":
            risk_logits = output.risk_level_logits[0, token_index]
            category_logits = output.category_logits[0, token_index]
            risk_map = self.model.response_risk_level_map
            category_map = self.model.response_category_map
        else:
            raise ValueError("role must be user or assistant")

        risk_probs = torch.softmax(risk_logits.float(), dim=-1)
        category_probs = torch.softmax(category_logits.float(), dim=-1)
        risk_probability, risk_index = torch.max(risk_probs, dim=-1)
        category_probability, category_index = torch.max(category_probs, dim=-1)
        severity = risk_map[int(risk_index)]
        category = category_map[int(category_index)] if severity != "Safe" else None
        return Assessment(
            severity=severity,
            category=category,
            risk_probability=round(float(risk_probability), 4),
            category_probability=round(float(category_probability), 4),
        )

    def moderate(self, messages: list[dict[str, str]]) -> tuple[Assessment, int, float]:
        if not messages:
            raise ValueError("messages must not be empty")
        role = messages[-1].get("role")
        if role not in {"user", "assistant"}:
            raise ValueError("the final message must be user or assistant")
        ids = self._token_ids(messages, generation_prompt=False)
        started = time.perf_counter()
        with self.lock, torch.inference_mode():
            output = self.model(
                input_ids=ids.unsqueeze(0),
                use_cache=False,
                logits_to_keep=1,
            )
            assessment = self._assessment(output, role)
        return assessment, int(ids.numel()), (time.perf_counter() - started) * 1000.0

    def start_stream(self, messages: list[dict[str, str]]) -> tuple[StreamState, float]:
        ids = self._token_ids(messages, generation_prompt=True)
        user_end = self._last_user_end(ids)
        started = time.perf_counter()
        with self.lock, torch.inference_mode():
            cache = DynamicCache()
            output = self.model(
                input_ids=ids.unsqueeze(0),
                past_key_values=cache,
                use_cache=True,
                logits_to_keep=0,
            )
            assessment = self._assessment(output, "user", user_end)
        state = StreamState(
            past_key_values=output.past_key_values,
            token_count=int(ids.numel()),
            assessment=assessment,
        )
        return state, (time.perf_counter() - started) * 1000.0

    def step_stream(self, state: StreamState, text: str) -> tuple[Assessment, int, float]:
        token_ids = self.tokenizer.encode(text, add_special_tokens=False)
        if state.token_count + len(token_ids) > MAX_TOKENS:
            raise ValueError(f"guard stream exceeds {MAX_TOKENS} tokens")
        if not token_ids:
            return state.assessment, 0, 0.0

        worst = state.assessment
        rank = {"Safe": 0, "Controversial": 1, "Unsafe": 2}
        started = time.perf_counter()
        with self.lock, torch.inference_mode():
            tokens = torch.tensor([token_ids], dtype=torch.long)
            output = self.model(
                input_ids=tokens,
                past_key_values=state.past_key_values,
                use_cache=True,
                logits_to_keep=0,
            )
            state.past_key_values = output.past_key_values
            state.token_count += len(token_ids)
            for token_index in range(len(token_ids)):
                current = self._assessment(output, "assistant", token_index)
                if rank[current.severity] >= rank[worst.severity]:
                    worst = current
                if current.flagged:
                    break
        state.assessment = worst
        return worst, len(token_ids), (time.perf_counter() - started) * 1000.0

    def finish_stream(self, state: StreamState) -> tuple[Assessment, float]:
        started = time.perf_counter()
        with self.lock, torch.inference_mode():
            token = torch.tensor([[self.im_end_id]], dtype=torch.long)
            output = self.model(
                input_ids=token,
                past_key_values=state.past_key_values,
                use_cache=True,
                logits_to_keep=1,
            )
            state.past_key_values = output.past_key_values
            state.token_count += 1
            assessment = self._assessment(output, "assistant")
        state.assessment = assessment
        return assessment, (time.perf_counter() - started) * 1000.0


def _normalized_messages(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("messages must be an array")
    messages: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in {"system", "user", "assistant"} and isinstance(content, str) and content:
            messages.append({"role": role, "content": content})
    if not messages:
        raise ValueError("no text messages are available for moderation")
    return messages


ENGINE = GuardEngine()
STREAM_SLOTS = asyncio.Semaphore(MAX_STREAMS)
app = FastAPI(title="Qwen3Guard Stream CPU", version="0.1.0", docs_url=None, redoc_url=None)


def _authorized(value: str) -> bool:
    return bool(API_KEY) and secrets.compare_digest(value, f"Bearer {API_KEY}")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "status": "ok",
        "model": MODEL_ID,
        "device": "cpu",
        "dtype": ENGINE.dtype,
        "threads": TORCH_THREADS,
        "max_streams": MAX_STREAMS,
        "load_seconds": ENGINE.load_seconds,
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if not _authorized(request.headers.get("Authorization", "")):
        return JSONResponse(status_code=401, content={"error": {"message": "Invalid API key"}})
    try:
        payload = await request.json()
        messages = _normalized_messages(payload.get("messages"))
        assessment, token_count, elapsed_ms = await asyncio.to_thread(ENGINE.moderate, messages)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})
    return {
        "id": f"guard-{time.time_ns()}",
        "object": "chat.completion",
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": assessment.as_guard_text()},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": token_count, "completion_tokens": 0, "total_tokens": token_count},
        "guard": {**assessment.as_dict(), "inference_ms": round(elapsed_ms, 3)},
    }


@app.post("/v1/moderations")
async def moderations(request: Request):
    if not _authorized(request.headers.get("Authorization", "")):
        return JSONResponse(status_code=401, content={"error": {"message": "Invalid API key"}})
    try:
        payload = await request.json()
        if isinstance(payload.get("messages"), list):
            messages = _normalized_messages(payload["messages"])
        else:
            text = payload.get("input")
            if not isinstance(text, str) or not text:
                raise ValueError("input must be a non-empty string")
            messages = [{"role": "user", "content": text}]
        assessment, token_count, elapsed_ms = await asyncio.to_thread(ENGINE.moderate, messages)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})
    return {
        "id": f"modr-{time.time_ns()}",
        "model": MODEL_ID,
        "results": [{**assessment.as_dict(), "tokens": token_count, "inference_ms": round(elapsed_ms, 3)}],
    }


@app.websocket("/v1/moderations/stream")
async def moderation_stream(websocket: WebSocket):
    if not _authorized(websocket.headers.get("Authorization", "")):
        await websocket.close(code=4401, reason="Invalid API key")
        return
    await websocket.accept()
    state: StreamState | None = None
    async with STREAM_SLOTS:
        try:
            while True:
                payload = json.loads(await websocket.receive_text())
                event_type = payload.get("type")
                if event_type == "start":
                    if state is not None:
                        raise ValueError("stream is already started")
                    messages = _normalized_messages(payload.get("messages"))
                    state, elapsed_ms = await asyncio.to_thread(ENGINE.start_stream, messages)
                    await websocket.send_json(
                        {
                            "type": "assessment",
                            "stage": "input",
                            "assessment": state.assessment.as_dict(),
                            "tokens": state.token_count,
                            "inference_ms": round(elapsed_ms, 3),
                        }
                    )
                elif event_type == "delta":
                    if state is None:
                        raise ValueError("stream has not been started")
                    text = payload.get("text")
                    if not isinstance(text, str):
                        raise ValueError("delta text must be a string")
                    assessment, tokens, elapsed_ms = await asyncio.to_thread(ENGINE.step_stream, state, text)
                    await websocket.send_json(
                        {
                            "type": "assessment",
                            "stage": "output",
                            "assessment": assessment.as_dict(),
                            "tokens": tokens,
                            "total_tokens": state.token_count,
                            "inference_ms": round(elapsed_ms, 3),
                        }
                    )
                elif event_type == "finish":
                    if state is None:
                        raise ValueError("stream has not been started")
                    assessment, elapsed_ms = await asyncio.to_thread(ENGINE.finish_stream, state)
                    await websocket.send_json(
                        {
                            "type": "assessment",
                            "stage": "final",
                            "assessment": assessment.as_dict(),
                            "total_tokens": state.token_count,
                            "inference_ms": round(elapsed_ms, 3),
                        }
                    )
                    return
                else:
                    raise ValueError("unknown stream event")
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            try:
                await websocket.send_json({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
                await websocket.close(code=1011)
            except (RuntimeError, WebSocketDisconnect):
                pass
        finally:
            if state is not None:
                state.past_key_values = None
