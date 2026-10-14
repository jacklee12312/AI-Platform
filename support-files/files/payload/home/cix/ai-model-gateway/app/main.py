from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

import httpx
import anyio
import websockets
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from websockets.exceptions import ConnectionClosed

from .chat import normalize_tool_history
from .config import ModelEntry, Registry
from .defaults import apply_generation_defaults
from .errors import GatewayError, error_payload
from .guard import GuardAssessment, messages_for_guard, moderation_result, parse_guard_output
from .streaming import sse_event, stream_assessment
from .upstream import UpstreamError, request_bytes, request_json, request_json_async
from .usage import APIKeyUsageStore
from .request_diagnostics import (
    finish_diagnostics,
    new_diagnostics,
    upstream_code,
)
from .request_content import (
    clean_request_metadata,
    sanitize_request_content,
    unavailable_request_content,
)
from .runtime_safety import RuntimeSafety
from .access import (AdmissionController, AdmissionLifetimeMiddleware, KeyPolicy, ResourcePolicy,
                     authorize, cancellable, current_lease, expired, model_pools, upstream_thread)


VERSION = "0.9.0"
REGISTRY = Registry(
    os.getenv("MODEL_REGISTRY", "/app/config/models.json"),
    mode_path=os.getenv("MODEL_MODE_FILE", "/app/state/active-mode"),
    default_mode=os.getenv("MODEL_MODE_DEFAULT", "copaw"),
    installation_path=os.getenv("MODEL_INSTALLATION_FILE", "/app/state/model-installations.json"),
)
API_KEY = os.getenv("GATEWAY_API_KEY") or os.getenv("HOUMO_API_KEY", "")
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY") or os.getenv("HOUMO_API_KEY", "")
MANAGEMENT_TOKEN = os.getenv("GATEWAY_MANAGEMENT_TOKEN", "")
USAGE_DB_PATH = os.getenv("GATEWAY_USAGE_DB", "/app/data/gateway-usage.db")
METERED_ENDPOINTS = {
    "/v1/chat/completions", "/v1/embeddings", "/v1/rerank", "/v1/moderations",
    "/v1/ocr/mineru", "/v1/audio/speech", "/v1/audio/tts",
    "/v1/audio/transcriptions", "/v1/audio/asr",
}


def _is_metered(path: str) -> bool:
    return path in METERED_ENDPOINTS or path.startswith("/v1/cv/")
GUARD_MODE = os.getenv("GUARD_MODE", "off").lower()
GUARD_FAIL_CLOSED = os.getenv("GUARD_FAIL_CLOSED", "true").lower() in {"1", "true", "yes", "on"}
GUARD_MODEL_ID = os.getenv("GUARD_MODEL_ID", "qwen3guard-stream-0.6b")
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "120"))
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(25 * 1024 * 1024)))
CHAT_TIMEOUT = float(os.getenv("CHAT_TIMEOUT_SECONDS", "300"))
GUARD_TIMEOUT = float(os.getenv("GUARD_TIMEOUT_SECONDS", "90"))
STREAM_GUARD_TIMEOUT = float(os.getenv("STREAM_GUARD_TIMEOUT_SECONDS", "30"))
STREAM_GUARD_BATCH_CHUNKS = max(1, int(os.getenv("STREAM_GUARD_BATCH_CHUNKS", "8")))
CV_TIMEOUT = float(os.getenv("CV_TIMEOUT_SECONDS", "180"))
AUDIO_TIMEOUT = float(os.getenv("AUDIO_TIMEOUT_SECONDS", "600"))
AUDIO_MAX_BODY_BYTES = int(os.getenv("AUDIO_MAX_BODY_BYTES", str(100 * 1024 * 1024)))
GENERATION_DEFAULTS_ENABLED = os.getenv("GENERATION_DEFAULTS_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "1024"))
DEFAULT_TEMPERATURE = float(os.getenv("DEFAULT_TEMPERATURE", "0.2"))
DEFAULT_TOP_P = float(os.getenv("DEFAULT_TOP_P", "0.9"))
CV_DEFAULT_CONFIDENCE = float(os.getenv("CV_DEFAULT_CONFIDENCE", "0.25"))
CV_DEFAULT_IOU = float(os.getenv("CV_DEFAULT_IOU", "0.45"))
USAGE_STORE = APIKeyUsageStore(USAGE_DB_PATH)
RUNTIME_SAFETY = RuntimeSafety(os.getenv('RUNTIME_SAFETY_FILE', '/app/state/runtime-safety.json'),
    required=os.getenv('RUNTIME_SAFETY_REQUIRED', 'false').lower() == 'true')


def log_event(event: str, **fields: Any) -> None:
    print(json.dumps({"ts": time.time(), "event": event, **fields}, ensure_ascii=False, separators=(",", ":")), flush=True)


class FixedWindowLimiter:
    def __init__(self, limit: int):
        self.limit = limit
        self.events: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str, limit: int | None = None) -> bool:
        now = time.monotonic()
        with self.lock:
            queue = self.events[key]
            while queue and queue[0] <= now - 60.0:
                queue.popleft()
            if len(queue) >= (self.limit if limit is None else limit):
                return False
            queue.append(now)
            return True


LIMITER = FixedWindowLimiter(RATE_LIMIT_PER_MINUTE)
ADMISSION = AdmissionController()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(USAGE_STORE.initialize, API_KEY)
    yield


app = FastAPI(title="AI Model Gateway", version=VERSION, docs_url="/docs", redoc_url=None, lifespan=lifespan)

if GUARD_MODE not in {"block", "audit", "off"}:
    raise RuntimeError("GUARD_MODE must be one of: block, audit, off")


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", uuid.uuid4().hex)


def _management_authorized(request: Request) -> bool:
    provided = request.headers.get("X-Gateway-Admin-Token", "")
    return bool(MANAGEMENT_TOKEN and provided and secrets.compare_digest(provided, MANAGEMENT_TOKEN))


def _business_key(request: Request) -> dict[str, Any] | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return USAGE_STORE.authenticate(auth[7:], include_expired=True)


async def _admit(request: Request | WebSocket, entry: ModelEntry) -> None:
    request.state.usage_model = entry.id
    diagnostics = getattr(request.state, "diagnostics", {})
    diagnostics["stage"] = "admission"
    key = getattr(request.state, "api_key_record", None)
    policy = key["policy"] if key else {}
    if key:
        authorize(policy, entry.id)
    await asyncio.to_thread(RUNTIME_SAFETY.require, entry)
    resources = await asyncio.to_thread(USAGE_STORE.get_resource_policy)
    if not hasattr(request.state, "usage_event_id"):
        request.state.usage_event_id = uuid.uuid4().hex
    metadata = getattr(request.state, "request_metadata", {})
    metadata["application"] = policy.get("application", "")
    lease = ADMISSION.acquire(key["id"] if key else "management-console", entry.id,
                              model_pools(entry.metadata), policy, resources,
                              metadata={
                                  "id": request.state.usage_event_id,
                                  "state": request.scope.get("state", {}),
                                  "key_name": key["name"] if key else "管理台测试",
                                  "endpoint": request.url.path,
                                  "cancel_supported": entry.capability == "chat",
                              })
    request.scope["admission_lease"] = lease
    try:
        if key:
            await asyncio.to_thread(USAGE_STORE.reserve_key_request, key["id"], entry.id)
    except BaseException:
        lease.finish()
        raise
    model_dimension = ("model", entry.id)
    diagnostics.update(
        scope="model",
        in_flight=lease.in_flight.get(model_dimension, 1),
        limit=lease.limits.get(model_dimension, 0),
    )
    diagnostics.pop("upstream_dispatched", None)


async def _access_rejection(request: Request | WebSocket, code: str) -> None:
    if code in {"api_key_expired", "model_not_allowed", "rate_limit_exceeded", "daily_quota_exceeded"} or code.endswith("_concurrency_exceeded"):
        await asyncio.to_thread(USAGE_STORE.record_rejection, getattr(request.state, "api_key_id", None),
                                _request_id(request), getattr(request.state, "usage_model", "unknown"), code)


async def _record_usage(
    request: Request,
    *,
    endpoint: str,
    model: str,
    usage: Any,
    status: str = "succeeded",
) -> None:
    if status == "succeeded":
        getattr(request.state, "diagnostics", {}).setdefault("http_status", 200)
    await asyncio.to_thread(
        USAGE_STORE.record_usage,
        api_key_id=getattr(request.state, "api_key_id", None),
        request_id=_request_id(request),
        endpoint=endpoint,
        model=model,
        usage=usage,
        status=status,
        event_id=getattr(request.state, "usage_event_id", None),
        diagnostics=finish_diagnostics(getattr(request.state, "diagnostics", {})),
        request_content=getattr(request.state, "request_content", None),
        request_metadata=getattr(request.state, "request_metadata", None),
    )
    request.state.usage_recorded = True


def _error_response(request: Request, error: GatewayError) -> JSONResponse:
    diagnostics = getattr(request.state, "diagnostics", {})
    diagnostics.update(code=error.code, http_status=error.status_code)
    if isinstance(error.details, dict):
        concurrency_details = error.details.get("scope") in {"global", "key", "model", "pool"}
        for name in (("scope", "limit", "in_flight") if concurrency_details else ()):
            if name in error.details:
                diagnostics[name] = error.details[name]
        if "retry_after" in error.details:
            diagnostics["retry_after"] = error.details["retry_after"]
        if error.details.get("stage") in {"input", "output"}:
            diagnostics["stage"] = "guard"
        if error.details.get("status") is not None:
            diagnostics.update(
                upstream_dispatched=True,
                upstream_http_status=error.details["status"],
                stage="upstream",
            )
        diagnostics["code"] = upstream_code(error.details.get("body"), error.code)
    headers = {"X-Request-ID": _request_id(request)}
    if error.status_code == 429:
        headers["Retry-After"] = str((error.details or {}).get("retry_after", 60))
    return JSONResponse(
        status_code=error.status_code,
        content=error_payload(error, _request_id(request)),
        headers=headers,
    )


@app.middleware("http")
async def gateway_middleware(request: Request, call_next):
    request.state.request_id = request.headers.get("X-Request-ID", "")[:128] or uuid.uuid4().hex
    request.state.api_key_id = None
    request.state.api_key_name = None
    request.state.api_key_record = None
    request.state.usage_event_id = uuid.uuid4().hex  # Never deduplicate caller-supplied Request IDs.
    request.state.usage_recorded = False
    request.state.diagnostics = new_diagnostics()
    _initialize_request_content(request)
    started = time.perf_counter()
    path = request.url.path
    if path.startswith("/v1"):
        key_record = None if _management_authorized(request) else await asyncio.to_thread(_business_key, request)
        if not _management_authorized(request) and key_record is None:
            response = _error_response(
                request,
                GatewayError(401, "Invalid or missing API key", "invalid_api_key", "authentication_error"),
            )
            response.headers["WWW-Authenticate"] = "Bearer"
            log_event("request", request_id=_request_id(request), method=request.method, path=path, status=401)
            return response
        if key_record is not None:
            request.state.api_key_id = key_record["id"]
            request.state.api_key_name = key_record["name"]
            request.state.api_key_record = key_record
            if expired(key_record["policy"]):
                await _access_rejection(request, "api_key_expired")
                response = _error_response(request, GatewayError(401, "API Key 已到期，请联系管理员续期", "api_key_expired", "authentication_error"))
                if _is_metered(path):
                    await _record_usage(request, endpoint=path, model="unknown", usage=None, status="rejected")
                return response
        limiter_key = key_record["id"] if key_record is not None else "management-console"
        key_limit = key_record["policy"]["rate_limit_per_minute"] if key_record else 0
        if not LIMITER.allow(limiter_key, key_limit or None):
            response = _error_response(
                request,
                GatewayError(429, "Rate limit exceeded", "rate_limit_exceeded", "rate_limit_error"),
            )
            response.headers["Retry-After"] = "60"
            await _access_rejection(request, "rate_limit_exceeded")
            if _is_metered(path):
                await _record_usage(request, endpoint=path, model="unknown", usage=None, status="rejected")
            log_event("request", request_id=_request_id(request), method=request.method, path=path, status=429)
            return response
    try:
        response = await call_next(request)
    except GatewayError as error:
        await _access_rejection(request, error.code)
        response = _error_response(request, error)
    except Exception as exc:
        log_event("unhandled_error", request_id=_request_id(request), error=f"{type(exc).__name__}: {exc}")
        response = _error_response(
            request,
            GatewayError(500, "Internal gateway error", "internal_error", "internal_server_error"),
        )
    if response.status_code >= 400 and _is_metered(path) and not request.state.usage_recorded:
        await _record_usage(request, endpoint=path, model=getattr(request.state, "usage_model", "unknown"),
                            usage=None, status="cancelled" if request.state.diagnostics.get("code") == "admin_cancelled" else "failed" if response.status_code >= 500 else "rejected")
    response.headers["X-Request-ID"] = _request_id(request)
    if path != "/health/live":
        log_event(
            "request",
            request_id=_request_id(request),
            method=request.method,
            path=path,
            status=response.status_code,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
        )
    return response


def _upstream_headers(content_type: str = "application/json") -> dict[str, str]:
    headers = {"Content-Type": content_type}
    if UPSTREAM_API_KEY:
        headers["Authorization"] = f"Bearer {UPSTREAM_API_KEY}"
    return headers


async def _call_json(
    url: str,
    *,
    method: str = "GET",
    payload: Any = None,
    raw_body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    diagnostics: dict | None = None,
) -> dict:
    entry = next((entry for entry in REGISTRY.entries() if entry.base_url and
                  url.startswith(entry.base_url.rstrip('/') + '/')), None)
    if entry:
        await asyncio.to_thread(RUNTIME_SAFETY.require, entry)
    if diagnostics is not None:
        diagnostics["stage"] = "upstream"
        # A connection attempt does not prove that the model accepted work.
        diagnostics.pop("upstream_dispatched", None)
    try:
        arguments = dict(
            method=method,
            payload=payload,
            raw_body=raw_body,
            headers=headers,
            timeout=timeout,
        )
        lease = current_lease()
        if lease and lease.cancel_supported and url.endswith("/v1/chat/completions"):
            result = await cancellable(request_json_async(url, **arguments), lease)
        else:
            result = await upstream_thread(request_json, url, **arguments)
        if entry:
            await asyncio.to_thread(RUNTIME_SAFETY.require, entry)
        if diagnostics is not None:
            diagnostics.update(upstream_dispatched=True, upstream_http_status=200)
        return result
    except UpstreamError as exc:
        status = 503 if exc.status_code is None else 502
        default_code = "upstream_unavailable" if exc.status_code is None else "upstream_error"
        code = upstream_code(f"{exc.message} {exc.body or ''}", default_code)
        if diagnostics is not None:
            diagnostics["code"] = code
            if exc.status_code is not None:
                diagnostics.update(
                    upstream_dispatched=True,
                    upstream_http_status=exc.status_code,
                )
        raise GatewayError(
            status,
            exc.message,
            code,
            "service_unavailable_error" if status == 503 else "upstream_error",
            details={"status": exc.status_code, "body": exc.body[:500] if exc.body else None},
        ) from exc


def _initialize_request_content(request: Request | WebSocket) -> None:
    request.state.request_content = unavailable_request_content()
    metadata = {
        "client_ip": request.client.host if request.client else "",
        "client_port": request.client.port if request.client else 0,
        "user_agent": request.headers.get("user-agent", "")[:256],
        "method": request.scope.get("method", "WEBSOCKET"),
        "content_type": request.headers.get("content-type", "")[:128],
    }
    request.state.request_metadata = clean_request_metadata(metadata)
    length = request.headers.get("content-length", "")
    if length.isdigit():
        request.state.request_metadata["raw_bytes"] = int(length[:12])
    if metadata["content_type"].startswith(
        ("multipart/", "image/", "audio/", "application/octet-stream")
    ):
        _capture_request_content(
            request,
            {
                "media": {
                    "content_type": metadata["content_type"],
                    "bytes": request.state.request_metadata.get("raw_bytes"),
                    "notice": "文件二进制内容未保存",
                }
            },
        )


def _capture_request_content(
    request: Request | WebSocket, payload: object, raw_size: int | None = None
) -> None:
    request.state.request_content = sanitize_request_content(payload, raw_size=raw_size)
    metadata = getattr(request.state, "request_metadata", {})
    if raw_size is not None:
        metadata["raw_bytes"] = raw_size
    if isinstance(payload, dict):
        if isinstance(payload.get("messages"), list):
            metadata["message_count"] = len(payload["messages"])
        if isinstance(payload.get("tools"), list):
            metadata["tool_count"] = len(payload["tools"])
        metadata["stream"] = bool(payload.get("stream", False))
    request.state.request_metadata = clean_request_metadata(metadata)


async def _read_json(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise GatewayError(413, "Request body is too large", "request_too_large", "invalid_request_error")
    _capture_request_content(
        request,
        {"invalid_json_preview": raw.decode("utf-8", errors="replace")},
        len(raw),
    )
    try:
        value = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise GatewayError(400, "Request body must be valid JSON", "invalid_json", "invalid_request_error") from exc
    if not isinstance(value, dict):
        raise GatewayError(400, "Request body must be a JSON object", "invalid_request", "invalid_request_error")
    request.state.usage_model = str(value.get("model") or "unknown")[:128]
    _capture_request_content(request, value, len(raw))
    return value


async def _moderate_messages(messages: list[dict], include_assistant: bool) -> GuardAssessment:
    guard = REGISTRY.get(GUARD_MODEL_ID, "moderation")
    sanitized = messages_for_guard(messages, include_assistant=include_assistant)
    if not sanitized:
        raise GatewayError(400, "No text content is available for safety review", "empty_guard_input", "invalid_request_error")
    payload = {
        "model": guard.upstream_model,
        "messages": sanitized,
        "temperature": 0,
        "max_tokens": 96,
    }
    try:
        response = await _call_json(
            f"{guard.base_url}/v1/chat/completions",
            method="POST",
            payload=payload,
            headers=_upstream_headers(),
            timeout=GUARD_TIMEOUT,
        )
        content = response["choices"][0]["message"]["content"]
        try:
            return parse_guard_output(content)
        except ValueError:
            log_event("guard_parse_error", output=str(content)[:500])
            raise
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise GatewayError(502, "Qwen3Guard returned an invalid response", "invalid_guard_response", "upstream_error") from exc


async def _guard_or_fail(messages: list[dict], include_assistant: bool, stage: str) -> GuardAssessment | None:
    if GUARD_MODE == "off":
        return None
    try:
        assessment = await _moderate_messages(messages, include_assistant)
    except GatewayError:
        if GUARD_FAIL_CLOSED:
            raise
        log_event("guard_bypassed", stage=stage, reason="guard_unavailable")
        return None
    log_event("guard_assessment", stage=stage, severity=assessment.severity, categories=list(assessment.categories))
    if GUARD_MODE == "block" and assessment.flagged:
        raise GatewayError(
            403,
            f"Request blocked by safety policy during {stage} review",
            "safety_violation",
            "safety_error",
            details={"stage": stage, "severity": assessment.severity, "categories": list(assessment.categories)},
        )
    return assessment


async def _receive_stream_assessment(guard_ws: Any, expected_stage: str) -> tuple[GuardAssessment, dict[str, Any]]:
    raw = await asyncio.wait_for(guard_ws.recv(), timeout=STREAM_GUARD_TIMEOUT)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("stream Guard returned a non-object response")
    if value.get("type") == "error":
        raise RuntimeError(str(value.get("error", "stream Guard failed")))
    if value.get("type") != "assessment" or value.get("stage") != expected_stage:
        raise ValueError(f"unexpected stream Guard event: {value.get('type')}/{value.get('stage')}")
    return stream_assessment(value), value


async def _start_stream_guard(messages: list[dict]) -> tuple[Any, GuardAssessment, dict[str, Any]]:
    guard = REGISTRY.get(GUARD_MODEL_ID, "moderation")
    if not guard.base_url:
        raise GatewayError(503, "Stream Guard has no upstream URL", "guard_unavailable", "service_unavailable_error")
    ws_url = f"{guard.base_url}/v1/moderations/stream".replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    guard_ws = None
    try:
        headers = {"Authorization": f"Bearer {UPSTREAM_API_KEY}"} if UPSTREAM_API_KEY else None
        guard_ws = await websockets.connect(
            ws_url,
            additional_headers=headers,
            open_timeout=STREAM_GUARD_TIMEOUT,
            close_timeout=5,
            max_size=1024 * 1024,
        )
        await guard_ws.send(json.dumps({"type": "start", "messages": messages}, ensure_ascii=False))
        assessment, metadata = await _receive_stream_assessment(guard_ws, "input")
        return guard_ws, assessment, metadata
    except Exception as exc:
        if guard_ws is not None:
            await guard_ws.close()
        raise GatewayError(
            503,
            f"Stream Guard unavailable: {type(exc).__name__}: {exc}",
            "guard_unavailable",
            "service_unavailable_error",
        ) from exc


def _stream_error(message: str, code: str, request_id: str) -> dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": "safety_error" if code == "safety_violation" else "stream_error",
            "code": code,
        },
        "request_id": request_id,
    }


async def _guarded_chat_events(
    *,
    entry: ModelEntry,
    model_id: str,
    upstream_payload: dict[str, Any],
    guard_ws: Any | None,
    request_id: str,
    api_key_id: str | None,
    usage_event_id: str | None = None,
    diagnostics: dict | None = None,
    request_content: dict | None = None,
    request_metadata: dict | None = None,
):
    diagnostics = diagnostics if diagnostics is not None else new_diagnostics()
    diagnostics["http_status"] = 200  # SSE response headers; final outcome is separate.
    started = time.perf_counter()
    content_chunks = 0
    guard_tokens = 0
    guard_ms = 0.0
    blocked = False
    stream_status = "interrupted"
    usage_value: dict[str, Any] | None = None
    pending: list[tuple[dict[str, Any], str]] = []
    cancel_watcher: asyncio.Task | None = None

    async def review_pending() -> tuple[GuardAssessment | None, list[dict[str, Any]]]:
        nonlocal guard_tokens, guard_ms
        if not pending:
            return None, []
        events = [event for event, _ in pending]
        text = "".join(content for _, content in pending)
        pending.clear()
        if guard_ws is None:
            return None, events
        await guard_ws.send(json.dumps({"type": "delta", "text": text}, ensure_ascii=False))
        assessment, metadata = await _receive_stream_assessment(guard_ws, "output")
        guard_tokens += int(metadata.get("tokens") or 0)
        guard_ms += float(metadata.get("inference_ms") or 0.0)
        return assessment, events

    try:
        await asyncio.to_thread(RUNTIME_SAFETY.require, entry)
        timeout = httpx.Timeout(CHAT_TIMEOUT, connect=15.0, read=CHAT_TIMEOUT, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            diagnostics["stage"] = "upstream"
            diagnostics.pop("upstream_dispatched", None)
            async with client.stream(
                "POST",
                f"{entry.base_url}/v1/chat/completions",
                json=upstream_payload,
                headers=_upstream_headers(),
            ) as upstream:
                diagnostics.update(
                    upstream_dispatched=True,
                    upstream_http_status=upstream.status_code,
                )
                lease = current_lease()
                if lease and lease.cancel_supported:
                    async def close_cancelled_stream() -> None:
                        await lease.cancel_event.wait()
                        await upstream.aclose()

                    cancel_watcher = asyncio.create_task(close_cancelled_stream())
                if upstream.status_code >= 400:
                    stream_status = "upstream_error"
                    body = (await upstream.aread()).decode("utf-8", errors="replace")[:500]
                    diagnostics["code"] = upstream_code(body)
                    yield sse_event(_stream_error(f"Upstream returned HTTP {upstream.status_code}: {body}", "upstream_error", request_id))
                    yield sse_event("[DONE]")
                    return

                async for line in upstream.aiter_lines():
                    diagnostics["stage"] = "stream"
                    await asyncio.to_thread(RUNTIME_SAFETY.require, entry)
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        pending_assessment, reviewed_events = await review_pending()
                        if pending_assessment and GUARD_MODE == "block" and pending_assessment.flagged:
                            blocked = True
                            stream_status = "blocked"
                            yield sse_event(_stream_error("Response blocked by streaming safety review", "safety_violation", request_id))
                            yield sse_event("[DONE]")
                            return
                        for reviewed_event in reviewed_events:
                            reviewed_event["model"] = model_id
                            yield sse_event(reviewed_event)
                        if guard_ws is not None:
                            await guard_ws.send(json.dumps({"type": "finish"}))
                            final_assessment, metadata = await _receive_stream_assessment(guard_ws, "final")
                            guard_ms += float(metadata.get("inference_ms") or 0.0)
                            if GUARD_MODE == "block" and final_assessment.flagged:
                                blocked = True
                                stream_status = "blocked"
                                yield sse_event(_stream_error("Response blocked by streaming safety review", "safety_violation", request_id))
                        if not blocked:
                            stream_status = "succeeded"
                        yield sse_event("[DONE]")
                        return

                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        diagnostics["code"] = "invalid_upstream_response"
                        stream_status = "invalid_response"
                        yield sse_event(_stream_error("Upstream returned invalid SSE JSON", "invalid_upstream_response", request_id))
                        yield sse_event("[DONE]")
                        return

                    if isinstance(event.get("usage"), dict):
                        usage_value = event["usage"]
                    if event.get("error"):
                        diagnostics["code"] = upstream_code(event["error"])
                        stream_status = "upstream_error"
                        yield sse_event(event)
                        yield sse_event("[DONE]")
                        return

                    for choice in event.get("choices", []):
                        if not isinstance(choice, dict):
                            continue
                        if choice.get("finish_reason"):
                            diagnostics["finish_reason"] = choice["finish_reason"]
                        delta = choice.get("delta")
                        if isinstance(delta, dict) and any(
                            delta.get(key)
                            for key in (
                                "content",
                                "reasoning",
                                "reasoning_content",
                                "tool_calls",
                            )
                        ):
                            elapsed = round(
                                (
                                    time.perf_counter()
                                    - diagnostics.get("_started", started)
                                )
                                * 1000,
                                3,
                            )
                            diagnostics.setdefault("first_output_ms", elapsed)
                            diagnostics["last_output_ms"] = elapsed
                            diagnostics["output_chunks"] = (
                                diagnostics.get("output_chunks", 0) + 1
                            )

                    content = "".join(
                        str(delta)
                        for choice in event.get("choices", [])
                        if isinstance(choice, dict)
                        and isinstance(choice.get("delta"), dict)
                        and isinstance((delta := choice["delta"].get("content")), str)
                    )
                    if content:
                        pending.append((event, content))
                        content_chunks += 1
                        if len(pending) < STREAM_GUARD_BATCH_CHUNKS:
                            continue
                        output_assessment, reviewed_events = await review_pending()
                        if output_assessment and GUARD_MODE == "block" and output_assessment.flagged:
                            blocked = True
                            stream_status = "blocked"
                            log_event(
                                "stream_guard_block",
                                request_id=request_id,
                                severity=output_assessment.severity,
                                categories=list(output_assessment.categories),
                                content_chunks=content_chunks,
                            )
                            yield sse_event(_stream_error("Response blocked by streaming safety review", "safety_violation", request_id))
                            yield sse_event("[DONE]")
                            return
                        for reviewed_event in reviewed_events:
                            reviewed_event["model"] = model_id
                            yield sse_event(reviewed_event)
                        continue

                    pending_assessment, reviewed_events = await review_pending()
                    if pending_assessment and GUARD_MODE == "block" and pending_assessment.flagged:
                        blocked = True
                        stream_status = "blocked"
                        yield sse_event(_stream_error("Response blocked by streaming safety review", "safety_violation", request_id))
                        yield sse_event("[DONE]")
                        return
                    for reviewed_event in reviewed_events:
                        reviewed_event["model"] = model_id
                        yield sse_event(reviewed_event)
                    event["model"] = model_id
                    yield sse_event(event)
    except (asyncio.CancelledError, GeneratorExit):
        stream_status = "cancelled"
        lease = current_lease()
        diagnostics["code"] = (
            "admin_cancelled"
            if lease and lease.cancel_requested
            else "client_cancelled"
        )
        raise
    except GatewayError as exc:
        stream_status = exc.code
        diagnostics["code"] = exc.code
        yield sse_event(_stream_error(exc.message, exc.code, request_id))
        yield sse_event("[DONE]")
    except Exception as exc:
        lease = current_lease()
        if lease and lease.cancel_requested:
            stream_status = "cancelled"
            diagnostics["code"] = "admin_cancelled"
            yield sse_event(
                _stream_error(
                    "此请求已由管理员中断", "admin_cancelled", request_id
                )
            )
        else:
            stream_status = "stream_error"
            diagnostics["code"] = (
                "upstream_timeout"
                if isinstance(exc, httpx.TimeoutException)
                else "upstream_unavailable"
                if isinstance(exc, httpx.ConnectError)
                else "stream_unavailable"
            )
            log_event("chat_stream_error", request_id=request_id, error=f"{type(exc).__name__}: {exc}")
            yield sse_event(_stream_error("Streaming backend unavailable", "stream_unavailable", request_id))
        yield sse_event("[DONE]")
    finally:
        if blocked:
            diagnostics.update(code="safety_violation", stage="guard")
        elif stream_status == "interrupted":
            diagnostics["code"] = "stream_interrupted"
        with anyio.CancelScope(shield=True):
            if cancel_watcher is not None:
                cancel_watcher.cancel()
                await asyncio.gather(cancel_watcher, return_exceptions=True)
            await asyncio.to_thread(
                USAGE_STORE.record_usage,
                api_key_id=api_key_id,
                request_id=request_id,
                endpoint="/v1/chat/completions",
                model=model_id,
                usage=usage_value,
                status=stream_status,
                event_id=usage_event_id,
                diagnostics=finish_diagnostics(diagnostics),
                request_content=request_content,
                request_metadata=request_metadata,
            )
            # Persist before websocket cleanup: cancellation or a slow guard
            # close must not lose a business usage record.
            if guard_ws is not None:
                try:
                    await asyncio.wait_for(guard_ws.close(), timeout=5)
                except Exception:
                    pass
        log_event(
            "chat_stream",
            request_id=request_id,
            model=model_id,
            blocked=blocked,
            usage_reported=usage_value is not None,
            content_chunks=content_chunks,
            guard_tokens=guard_tokens,
            guard_ms=round(guard_ms, 3),
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
        )


async def _probe(entry: ModelEntry) -> dict:
    if not entry.enabled or not entry.health_url:
        return {"status": "unavailable", "reason": "not_deployed"}
    active_mode = REGISTRY.active_mode()
    if not REGISTRY.is_active(entry, active_mode):
        return {"status": "inactive", "reason": "not_in_active_mode", "active_mode": active_mode}
    safety = await asyncio.to_thread(RUNTIME_SAFETY.status, entry)
    if safety:
        return safety
    headers = _upstream_headers() if entry.backend == "openai" else {}
    try:
        started = time.perf_counter()
        await _call_json(entry.health_url, headers=headers, timeout=3.0)
        return {"status": "online", "latency_ms": round((time.perf_counter() - started) * 1000.0, 3)}
    except GatewayError as exc:
        return {"status": "error", "reason": exc.code}


async def _statuses() -> dict[str, dict]:
    entries = REGISTRY.entries()
    active_mode = REGISTRY.active_mode()
    unique: dict[tuple[str | None, str, bool], ModelEntry] = {}
    for entry in entries:
        unique[(entry.base_url, entry.backend, REGISTRY.is_active(entry, active_mode))] = entry
    results = await asyncio.gather(*(_probe(entry) for entry in unique.values()))
    by_backend = {key: value for key, value in zip(unique, results)}
    return {
        entry.id: by_backend[(entry.base_url, entry.backend, REGISTRY.is_active(entry, active_mode))]
        for entry in entries
    }


class APIKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    policy: KeyPolicy = Field(default_factory=KeyPolicy)


class APIKeyUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None
    policy: KeyPolicy | None = None


def _validate_policy_models(policy: KeyPolicy) -> None:
    unknown = set(policy.allowed_models or []) - set(REGISTRY.models)
    if unknown:
        raise GatewayError(400, "未知模型 ID：" + ", ".join(sorted(unknown)), "invalid_model_policy", "invalid_request_error")


@app.get("/internal/resource-policy")
async def internal_resource_policy(request: Request) -> dict:
    _require_management(request)
    policy = await asyncio.to_thread(USAGE_STORE.get_resource_policy)
    return {"policy": policy, "in_flight": ADMISSION.snapshot(), "default_rpm": RATE_LIMIT_PER_MINUTE,
            "models": [{"id": e.id, "capability": e.capability, "modes": list(e.modes), "pools": model_pools(e.metadata)} for e in REGISTRY.entries()],
            "recent_rejections": await asyncio.to_thread(USAGE_STORE.recent_rejections), "worker_scope": "single-process"}


@app.put("/internal/resource-policy")
async def internal_set_resource_policy(request: Request, payload: ResourcePolicy) -> dict:
    _require_management(request)
    if set(payload.model_limits) - set(REGISTRY.models):
        raise GatewayError(400, "并发策略包含未知模型 ID", "invalid_model_policy", "invalid_request_error")
    return {"policy": await asyncio.to_thread(USAGE_STORE.set_resource_policy, payload.model_dump())}


@app.get("/internal/requests/active")
async def active_requests(request: Request) -> JSONResponse:
    _require_management(request)
    policy = await asyncio.to_thread(USAGE_STORE.get_resource_policy)
    result = ADMISSION.request_snapshot()
    result["policy"] = policy
    result["models"] = [
        {"id": entry.id, "capability": entry.capability}
        for entry in REGISTRY.entries()
    ]
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.post("/internal/requests/{event_id}/cancel")
async def cancel_request(event_id: str, request: Request) -> JSONResponse:
    _require_management(request)
    result = ADMISSION.cancel(event_id)
    if result is None:
        historical = await asyncio.to_thread(USAGE_STORE.request_detail, event_id)
        if historical is None:
            raise GatewayError(
                404,
                "此请求已离开在途列表或不存在，请刷新状态",
                "request_not_found",
            )
        result = {
            "id": event_id,
            "status": "already_finished",
            "request_status": historical["status"],
            "message": "请求已经结束，无需中断",
        }
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    log_event(
        "admin_request_cancel",
        event_id=event_id,
        already_requested=result["already_requested"],
    )
    return JSONResponse(
        result, status_code=202, headers={"Cache-Control": "no-store"}
    )


@app.get("/internal/requests/{event_id}")
async def request_detail(event_id: str, request: Request) -> JSONResponse:
    _require_management(request)
    result = ADMISSION.request_detail(event_id)
    if result is None:
        result = await asyncio.to_thread(USAGE_STORE.request_detail, event_id)
    if result is None:
        raise GatewayError(
            404,
            "请求已结束且无历史记录，或记录不存在",
            "request_not_found",
            "invalid_request_error",
        )
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


def _require_management(request: Request) -> None:
    if not _management_authorized(request):
        raise GatewayError(401, "Invalid gateway management token", "invalid_management_token", "authentication_error")


@app.get("/internal/api-keys")
async def internal_api_keys(request: Request) -> dict[str, Any]:
    _require_management(request)
    return {"data": await asyncio.to_thread(USAGE_STORE.list_keys)}


@app.post("/internal/api-keys", status_code=201)
async def internal_create_api_key(request: Request, payload: APIKeyCreateRequest) -> dict[str, Any]:
    _require_management(request)
    _validate_policy_models(payload.policy)
    try:
        return await asyncio.to_thread(USAGE_STORE.create_key, payload.name, policy=payload.policy.model_dump())
    except ValueError as exc:
        raise GatewayError(400, str(exc), "invalid_api_key_name", "invalid_request_error", "name") from exc


@app.patch("/internal/api-keys/{key_id}")
async def internal_update_api_key(
    request: Request, key_id: str, payload: APIKeyUpdateRequest
) -> dict[str, Any]:
    _require_management(request)
    if payload.name is None and payload.enabled is None and payload.policy is None:
        raise GatewayError(400, "No API Key changes were supplied", "empty_update", "invalid_request_error")
    if payload.policy is not None:
        _validate_policy_models(payload.policy)
    try:
        return await asyncio.to_thread(
            USAGE_STORE.update_key,
            key_id,
            name=payload.name,
            enabled=payload.enabled,
            policy=payload.policy.model_dump(exclude_unset=True) if payload.policy is not None else None,
        )
    except KeyError as exc:
        raise GatewayError(404, "API Key was not found", "api_key_not_found", "invalid_request_error") from exc
    except ValueError as exc:
        raise GatewayError(400, str(exc), "invalid_api_key_update", "invalid_request_error") from exc


@app.delete("/internal/api-keys/{key_id}")
async def internal_revoke_api_key(request: Request, key_id: str) -> dict[str, Any]:
    _require_management(request)
    try:
        return await asyncio.to_thread(USAGE_STORE.revoke_key, key_id)
    except KeyError as exc:
        raise GatewayError(404, "API Key was not found or was already revoked", "api_key_not_found", "invalid_request_error") from exc


@app.get("/internal/usage")
async def internal_usage(
    request: Request,
    api_key_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    model: str | None = Query(default=None, max_length=128),
    request_id: str | None = Query(default=None, max_length=128),
    status: str | None = Query(default=None, max_length=128),
    error_code: str | None = Query(default=None, max_length=64),
) -> dict[str, Any]:
    _require_management(request)
    try:
        return await asyncio.to_thread(USAGE_STORE.usage_report, key_id=api_key_id, start=start, end=end,
                                       limit=limit, offset=offset, model=model, request_id=request_id,
                                       status=status, error_code=error_code)
    except ValueError as exc:
        raise GatewayError(400, str(exc), "invalid_usage_filter", "invalid_request_error") from exc


@app.get("/internal/usage/export")
async def internal_usage_export(
    request: Request,
    api_key_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    model: str | None = Query(default=None, max_length=128),
    request_id: str | None = Query(default=None, max_length=128),
    status: str | None = Query(default=None, max_length=128),
    error_code: str | None = Query(default=None, max_length=64),
) -> dict[str, Any]:
    _require_management(request)
    try:
        return await asyncio.to_thread(
            USAGE_STORE.export_usage_csv,
            key_id=api_key_id,
            start=start,
            end=end,
            model=model,
            request_id=request_id,
            status=status,
            error_code=error_code,
        )
    except ValueError as exc:
        raise GatewayError(400, str(exc), "invalid_usage_filter", "invalid_request_error") from exc


@app.get("/health/live")
async def live():
    enabled_keys = await asyncio.to_thread(USAGE_STORE.enabled_key_count)
    if enabled_keys < 1:
        return JSONResponse(status_code=503, content={"ok": False, "service": "ai-model-gateway", "version": VERSION, "error": "API key is not configured"})
    return {
        "ok": True,
        "service": "ai-model-gateway",
        "version": VERSION,
        "api_keys_enabled": enabled_keys,
        "management_configured": bool(MANAGEMENT_TOKEN),
        "guard_mode": GUARD_MODE,
        "generation_defaults_enabled": GENERATION_DEFAULTS_ENABLED,
    }


@app.get("/health")
async def health():
    statuses = await _statuses()
    online = sum(1 for status in statuses.values() if status["status"] == "online")
    errors = sum(1 for status in statuses.values() if status["status"] == "error")
    inactive = sum(1 for status in statuses.values() if status["status"] == "inactive")
    unavailable = sum(1 for status in statuses.values() if status["status"] == "unavailable")
    return {
        "ok": errors == 0,
        "status": "ok" if errors == 0 else "degraded",
        "service": "ai-model-gateway",
        "version": VERSION,
        "active_mode": REGISTRY.active_mode(),
        "guard_mode": GUARD_MODE,
        "generation_defaults_enabled": GENERATION_DEFAULTS_ENABLED,
        "online_models": online,
        "inactive_models": inactive,
        "unavailable_models": unavailable,
        "error_models": errors,
        "total_models": len(statuses),
        "models": statuses,
    }


@app.get("/v1/models")
async def models(request: Request):
    statuses = await _statuses()
    active_mode = REGISTRY.active_mode()
    data = []
    for entry in REGISTRY.entries():
        key = getattr(request.state, "api_key_record", None)
        allowed = key["policy"]["allowed_models"] if key else None
        if allowed is not None and entry.id not in allowed:
            continue
        data.append({
            "id": entry.id,
            "object": "model",
            "created": 0,
            "owned_by": "local-gateway",
            "capability": entry.capability,
            "status": statuses[entry.id]["status"],
            "safety": statuses[entry.id] if statuses[entry.id].get('reason', '').startswith('npu_') else None,
            "backend": entry.backend,
            "guarded": bool(entry.guard and GUARD_MODE != "off"),
            "modes": list(entry.modes),
            "active_mode": active_mode,
            "metadata": entry.metadata,
        })
    return {"object": "list", "data": data}


async def _cancellable_chat_events(**kwargs: Any):
    iterator = _guarded_chat_events(**kwargs)
    lease = current_lease()
    try:
        while True:
            try:
                yield await cancellable(iterator.__anext__(), lease)
            except StopAsyncIteration:
                return
    except GatewayError as exc:
        diagnostics = kwargs["diagnostics"]
        diagnostics.update(code=exc.code, http_status=200)
        with anyio.CancelScope(shield=True):
            await asyncio.to_thread(
                USAGE_STORE.record_usage,
                api_key_id=kwargs.get("api_key_id"),
                request_id=kwargs["request_id"],
                endpoint="/v1/chat/completions",
                model=kwargs["model_id"],
                usage=None,
                status="cancelled",
                event_id=kwargs.get("usage_event_id"),
                diagnostics=finish_diagnostics(diagnostics),
                request_content=kwargs.get("request_content"),
                request_metadata=kwargs.get("request_metadata"),
            )
        yield sse_event(_stream_error(exc.message, exc.code, kwargs["request_id"]))
        yield sse_event("[DONE]")
    finally:
        with anyio.CancelScope(shield=True):
            await iterator.aclose()


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    payload = await _read_json(request)
    model_id = str(payload.get("model", ""))
    entry = REGISTRY.get(model_id, "chat")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GatewayError(400, "messages must be a non-empty array", "invalid_messages", "invalid_request_error", "messages")
    upstream_messages = normalize_tool_history(messages)
    upstream_payload, defaults_applied = apply_generation_defaults(
        payload,
        enabled=GENERATION_DEFAULTS_ENABLED,
        max_tokens=DEFAULT_MAX_TOKENS,
        temperature=DEFAULT_TEMPERATURE,
        top_p=DEFAULT_TOP_P,
    )
    upstream_payload["model"] = entry.upstream_model
    upstream_payload["messages"] = upstream_messages
    request.state.request_metadata["effective_parameters"] = clean_request_metadata(
        {"effective_parameters": upstream_payload}
    ).get("effective_parameters", {})
    await _admit(request, entry)
    if defaults_applied:
        log_event("generation_defaults", request_id=_request_id(request), model=model_id, fields=defaults_applied)

    if payload.get("stream"):
        stream_options = upstream_payload.get("stream_options")
        stream_options = dict(stream_options) if isinstance(stream_options, dict) else {}
        stream_options["include_usage"] = True
        upstream_payload["stream_options"] = stream_options
        input_assessment = None
        guard_ws = None
        if entry.guard and GUARD_MODE != "off":
            request.state.diagnostics.update(stage="guard", upstream_dispatched=False)
            sanitized = messages_for_guard(upstream_messages, include_assistant=False)
            if not sanitized:
                raise GatewayError(400, "No text content is available for safety review", "empty_guard_input", "invalid_request_error")
            try:
                guard_ws, input_assessment, metadata = await _start_stream_guard(sanitized)
                log_event(
                    "guard_assessment",
                    stage="input_stream",
                    severity=input_assessment.severity,
                    categories=list(input_assessment.categories),
                    inference_ms=metadata.get("inference_ms"),
                    tokens=metadata.get("tokens"),
                )
            except GatewayError:
                if GUARD_FAIL_CLOSED:
                    raise
                log_event("guard_bypassed", stage="input_stream", reason="guard_unavailable")
            if input_assessment and GUARD_MODE == "block" and input_assessment.flagged:
                if guard_ws is not None:
                    await guard_ws.close()
                raise GatewayError(
                    403,
                    "Request blocked by safety policy during input review",
                    "safety_violation",
                    "safety_error",
                    details={
                        "stage": "input",
                        "severity": input_assessment.severity,
                        "categories": list(input_assessment.categories),
                    },
                )
        return StreamingResponse(
            _cancellable_chat_events(
                entry=entry,
                model_id=model_id,
                upstream_payload=upstream_payload,
                guard_ws=guard_ws,
                request_id=_request_id(request),
                api_key_id=getattr(request.state, "api_key_id", None),
                usage_event_id=getattr(request.state, "usage_event_id", None),
                diagnostics=request.state.diagnostics,
                request_content=request.state.request_content,
                request_metadata=request.state.request_metadata,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "X-Guard-Input": input_assessment.severity if input_assessment else "Skipped",
                "X-Guard-Output": "Streaming" if guard_ws is not None else "Skipped",
                "X-Generation-Defaults": ",".join(defaults_applied) or "None",
            },
        )

    input_assessment = None
    if entry.guard:
        request.state.diagnostics.update(stage="guard", upstream_dispatched=False)
        input_assessment = await _guard_or_fail(upstream_messages, False, "input")
    response = await _call_json(
        f"{entry.base_url}/v1/chat/completions",
        method="POST",
        payload=upstream_payload,
        headers=_upstream_headers(),
        timeout=CHAT_TIMEOUT,
        diagnostics=request.state.diagnostics,
    )
    output_assessment = None
    if entry.guard:
        request.state.diagnostics["stage"] = "guard"
        try:
            assistant_content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise GatewayError(502, "Chat backend returned an invalid response", "invalid_upstream_response", "upstream_error") from exc
        output_messages = list(upstream_messages) + [{"role": "assistant", "content": assistant_content}]
        try:
            output_assessment = await _guard_or_fail(output_messages, True, "output")
        except GatewayError as exc:
            await _record_usage(
                request,
                endpoint="/v1/chat/completions",
                model=model_id,
                usage=response.get("usage"),
                status="blocked" if exc.code == "safety_violation" else "guard_error",
            )
            raise
    response["model"] = model_id
    reasons = [
        choice.get("finish_reason")
        for choice in response.get("choices", [])
        if isinstance(choice, dict) and choice.get("finish_reason")
    ]
    if reasons:
        request.state.diagnostics["finish_reason"] = reasons[0]
    await _record_usage(
        request,
        endpoint="/v1/chat/completions",
        model=model_id,
        usage=response.get("usage"),
    )
    headers = {
        "X-Guard-Input": input_assessment.severity if input_assessment else "Skipped",
        "X-Guard-Output": output_assessment.severity if output_assessment else "Skipped",
        "X-Generation-Defaults": ",".join(defaults_applied) or "None",
    }
    return JSONResponse(content=response, headers=headers)


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    payload = await _read_json(request)
    entry = REGISTRY.get(str(payload.get("model", "")), "embeddings")
    await _admit(request, entry)
    upstream_payload = dict(payload)
    upstream_payload["model"] = entry.upstream_model
    response = await _call_json(
        f"{entry.base_url}/v1/embeddings",
        method="POST",
        payload=upstream_payload,
        headers=_upstream_headers(),
        timeout=CHAT_TIMEOUT,
        diagnostics=request.state.diagnostics,
    )
    response["model"] = entry.id
    await _record_usage(
        request,
        endpoint="/v1/embeddings",
        model=entry.id,
        usage=response.get("usage"),
    )
    return response


@app.post("/v1/rerank")
async def rerank(request: Request):
    payload = await _read_json(request)
    entry = REGISTRY.get(str(payload.get("model", "")), "rerank")
    await _admit(request, entry)
    upstream_payload = dict(payload)
    upstream_payload["model"] = entry.upstream_model
    response = await _call_json(
        f"{entry.base_url}/v1/rerank",
        method="POST",
        payload=upstream_payload,
        headers=_upstream_headers(),
        timeout=CHAT_TIMEOUT,
        diagnostics=request.state.diagnostics,
    )
    await _record_usage(
        request,
        endpoint="/v1/rerank",
        model=entry.id,
        usage=response.get("usage"),
    )
    return response


@app.post("/v1/moderations")
async def moderations(request: Request):
    payload = await _read_json(request)
    model_id = str(payload.get("model") or GUARD_MODEL_ID)
    entry = REGISTRY.get(model_id, "moderation")
    value = payload.get("input")
    inputs = value if isinstance(value, list) else [value]
    if not inputs or any(not isinstance(item, str) or not item.strip() for item in inputs):
        raise GatewayError(400, "input must be a string or a non-empty string array", "invalid_input", "invalid_request_error", "input")
    await _admit(request, entry)
    assessments = await asyncio.gather(*(_moderate_messages([{"role": "user", "content": item}], False) for item in inputs))
    response = {
        "id": f"modr-{uuid.uuid4().hex}",
        "model": model_id,
        "results": [moderation_result(item) for item in assessments],
    }
    await _record_usage(request, endpoint="/v1/moderations", model=entry.id, usage=None)
    return response


CV_GROUPS = {
    "classify": ({"mobilenet_v2", "inception_v3"}, "mobilenet_v2"),
    "detect": ({"yolov11_l", "yolov11_n", "yolov11n_obb", "yolov12_s", "yolov12_m", "bytetrack_s"}, "yolov11_n"),
    "face": ({"retinaface", "yolov5s_face", "scrfd", "arcface"}, "retinaface"),
    "segment": ({"yolov8l_seg"}, "yolov8l_seg"),
}


@app.post("/v1/cv/{operation}")
async def cv_proxy(
    operation: str,
    request: Request,
    model: str | None = None,
    confidence: float = Query(CV_DEFAULT_CONFIDENCE, ge=0.0, le=1.0),
    iou: float = Query(CV_DEFAULT_IOU, ge=0.0, le=1.0),
):
    if operation not in CV_GROUPS:
        raise GatewayError(404, f"Unknown CV operation: {operation}", "operation_not_found", "invalid_request_error")
    allowed, default_model = CV_GROUPS[operation]
    model_id = model or default_model
    if model_id not in allowed:
        raise GatewayError(400, f"Model {model_id} is not valid for CV operation {operation}", "invalid_cv_model", "invalid_request_error", "model")
    entry = REGISTRY.get(model_id, f"cv_{operation}")
    body = await request.body()
    if not body:
        raise GatewayError(400, "Image body is required", "empty_image", "invalid_request_error")
    if len(body) > MAX_BODY_BYTES:
        raise GatewayError(413, "Image body is too large", "request_too_large", "invalid_request_error")
    _capture_request_content(
        request,
        {
            "model": model_id,
            "operation": operation,
            "content_type": request.headers.get("Content-Type", "application/octet-stream"),
            "binary_body": f"[已省略图片二进制，原始 {len(body)} 字节]",
            "confidence": confidence,
            "iou": iou,
        },
        len(body),
    )
    await _admit(request, entry)
    query = urlencode({"confidence": confidence, "iou": iou})
    content_type = request.headers.get("Content-Type", "application/octet-stream")
    response = await _call_json(
        f"{entry.base_url}/v1/cv/infer/{model_id}?{query}",
        method="POST",
        raw_body=body,
        headers={"Content-Type": content_type},
        timeout=CV_TIMEOUT,
        diagnostics=request.state.diagnostics,
    )
    await _record_usage(request, endpoint=request.url.path, model=entry.id, usage=response.get("usage"))
    return response


@app.post("/v1/audio/speech")
@app.post("/v1/audio/tts")
async def audio_speech(request: Request):
    content_type = request.headers.get("Content-Type", "application/json")
    if not content_type.lower().startswith("application/json"):
        raise GatewayError(400, "Speech synthesis requires application/json", "invalid_content_type", "invalid_request_error")
    body = await request.body()
    if not body:
        raise GatewayError(400, "Speech synthesis request is required", "empty_audio_request", "invalid_request_error")
    if len(body) > MAX_BODY_BYTES:
        raise GatewayError(413, "Speech synthesis request is too large", "request_too_large", "invalid_request_error")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise GatewayError(400, "Speech synthesis request must be valid JSON", "invalid_json", "invalid_request_error") from exc
    if not isinstance(payload, dict):
        raise GatewayError(400, "Speech synthesis request must be a JSON object", "invalid_request", "invalid_request_error")
    requested_model = str(payload.get("model") or "kokoro-82m")
    _capture_request_content(request, payload, len(body))
    model_aliases = {
        "kokoro": "kokoro-82m",
        "tts-1": "kokoro-82m",
        "matcha": "matcha-zh",
    }
    model_id = model_aliases.get(requested_model, requested_model)
    entry = REGISTRY.get(model_id, "audio_tts")
    await _admit(request, entry)
    upstream_payload = dict(payload)
    upstream_payload["model"] = entry.upstream_model
    body = json.dumps(upstream_payload, ensure_ascii=False).encode("utf-8")
    try:
        result = await upstream_thread(
            request_bytes,
            f"{entry.base_url}/v1/audio/speech",
            method="POST",
            raw_body=body,
            headers=_upstream_headers(content_type),
            timeout=AUDIO_TIMEOUT,
        )
    except UpstreamError as exc:
        status = 503 if exc.status_code is None else 502
        raise GatewayError(
            status,
            exc.message,
            "upstream_unavailable" if status == 503 else "upstream_error",
            "service_unavailable_error" if status == 503 else "upstream_error",
            details={"status": exc.status_code, "body": exc.body[:500] if exc.body else None},
        ) from exc
    headers = dict(result.headers)
    headers["X-Gateway-TTS-Model"] = entry.id
    request.state.diagnostics.update(
        stage="upstream", upstream_dispatched=True, upstream_http_status=result.status_code,
        http_status=result.status_code,
    )
    await _record_usage(request, endpoint=request.url.path, model=entry.id, usage=None)
    return Response(content=result.body, status_code=result.status_code, media_type=result.content_type, headers=headers)


@app.post("/v1/audio/transcriptions")
@app.post("/v1/audio/asr")
async def audio_transcriptions(request: Request):
    entry = REGISTRY.get("sensevoice", "audio_asr")
    content_type = request.headers.get("Content-Type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise GatewayError(400, "Audio transcription requires multipart/form-data", "invalid_content_type", "invalid_request_error")
    body = await request.body()
    if not body:
        raise GatewayError(400, "Audio file is required", "empty_audio", "invalid_request_error")
    if len(body) > AUDIO_MAX_BODY_BYTES:
        raise GatewayError(413, "Audio request is too large", "request_too_large", "invalid_request_error")
    _capture_request_content(
        request,
        {
            "model": entry.id,
            "content_type": content_type.split(";", 1)[0],
            "audio_file": f"[已省略上传音频，原始 multipart 请求 {len(body)} 字节]",
        },
        len(body),
    )
    await _admit(request, entry)
    headers = _upstream_headers(content_type)
    headers["Accept"] = request.headers.get("Accept", "application/json")
    try:
        result = await upstream_thread(
            request_bytes,
            f"{entry.base_url}/v1/audio/transcriptions",
            method="POST",
            raw_body=body,
            headers=headers,
            timeout=AUDIO_TIMEOUT,
        )
    except UpstreamError as exc:
        status = 503 if exc.status_code is None else 502
        raise GatewayError(
            status,
            exc.message,
            "upstream_unavailable" if status == 503 else "upstream_error",
            "service_unavailable_error" if status == 503 else "upstream_error",
            details={"status": exc.status_code, "body": exc.body[:500] if exc.body else None},
        ) from exc
    request.state.diagnostics.update(
        stage="upstream", upstream_dispatched=True, upstream_http_status=result.status_code,
        http_status=result.status_code,
    )
    usage = None
    try:
        parsed = json.loads(result.body)
        usage = parsed.get("usage") if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    await _record_usage(request, endpoint=request.url.path, model=entry.id, usage=usage)
    return Response(content=result.body, status_code=result.status_code, media_type=result.content_type, headers=result.headers)


def _websocket_api_key(websocket: WebSocket) -> str:
    auth = websocket.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return websocket.query_params.get("api_key", "")


@app.websocket("/v1/audio/transcriptions/stream")
async def audio_transcription_stream(websocket: WebSocket):
    provided_key = _websocket_api_key(websocket)
    key_record = await asyncio.to_thread(USAGE_STORE.authenticate, provided_key, include_expired=True)
    if key_record is None:
        await websocket.close(code=4401, reason="Invalid or missing API key")
        return
    client = websocket.client.host if websocket.client else "unknown"
    websocket.state.api_key_record = key_record
    websocket.state.api_key_id = key_record["id"]
    websocket.state.request_id = uuid.uuid4().hex
    websocket.state.usage_model = "sensevoice"
    websocket.state.usage_event_id = uuid.uuid4().hex
    _initialize_request_content(websocket)
    websocket.state.request_content = unavailable_request_content(
        "尚未收到 WebSocket start 控制消息"
    )
    usage_event_id = websocket.state.usage_event_id
    request_content = websocket.state.request_content
    diagnostics = new_diagnostics()
    websocket.state.diagnostics = diagnostics
    if expired(key_record["policy"]):
        await _access_rejection(websocket, "api_key_expired")
        await websocket.close(code=4401, reason="API Key expired")
        return
    if not LIMITER.allow(key_record["id"], key_record["policy"]["rate_limit_per_minute"] or None):
        await _access_rejection(websocket, "rate_limit_exceeded")
        await websocket.close(code=4429, reason="Rate limit exceeded")
        return
    try:
        entry = REGISTRY.get("sensevoice", "audio_asr")
        await _admit(websocket, entry)
    except GatewayError as exc:
        await _access_rejection(websocket, exc.code)
        await websocket.accept()
        await websocket.send_json({"type": "error", "error": exc.message, "code": exc.code})
        await websocket.close(code={401: 4401, 403: 4403, 429: 4429}.get(exc.status_code, 4503))
        return

    upstream_url = f"{entry.base_url}/v1/audio/transcriptions/stream".replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    await websocket.accept()
    started = time.perf_counter()
    stream_status = "succeeded"
    audio_bytes = 0
    audio_frames = 0
    try:
        diagnostics.update(stage="upstream", upstream_dispatched=False)
        async with websockets.connect(
            upstream_url,
            additional_headers={"Authorization": f"Bearer {UPSTREAM_API_KEY}"},
            max_size=AUDIO_MAX_BODY_BYTES,
            open_timeout=10,
            close_timeout=5,
        ) as upstream:
            diagnostics.update(stage="stream", upstream_dispatched=True)
            async def client_to_upstream():
                nonlocal request_content, audio_bytes, audio_frames
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        await upstream.close()
                        return
                    if message.get("bytes") is not None:
                        audio_frames += 1
                        audio_bytes += len(message["bytes"])
                        await upstream.send(message["bytes"])
                    elif message.get("text") is not None:
                        try:
                            control = json.loads(message["text"])
                            if isinstance(control, dict) and control.get("type") == "start":
                                request_content = sanitize_request_content(
                                    {
                                        **control,
                                        "audio_stream": "[已省略 WebSocket PCM 音频帧]",
                                    },
                                    raw_size=len(message["text"].encode("utf-8")),
                                )
                                websocket.state.request_content = request_content
                                websocket.state.request_metadata["stream"] = True
                        except json.JSONDecodeError:
                            pass
                        await upstream.send(message["text"])

            async def upstream_to_client():
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (WebSocketDisconnect, ConnectionClosed):
        stream_status = "cancelled"
        diagnostics["code"] = "client_cancelled"
    except Exception as exc:
        stream_status = "stream_error"
        diagnostics["code"] = "stream_unavailable"
        log_event("asr_stream_error", client=client, error=f"{type(exc).__name__}: {exc}")
        try:
            await websocket.send_json({"type": "error", "error": "ASR streaming backend unavailable"})
        except RuntimeError:
            pass
    finally:
        if request_content.get("available"):
            request_content["payload"]["audio_stream"] = (
                f"[已省略 {audio_frames} 个 PCM 音频帧，共 {audio_bytes} 字节]"
            )
        try:
            await websocket.close()
        except RuntimeError:
            pass
        log_event("asr_stream", client=client, elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3))
        await asyncio.to_thread(
            USAGE_STORE.record_usage,
            api_key_id=key_record["id"],
            request_id=websocket.state.request_id,
            endpoint="/v1/audio/transcriptions/stream",
            model=entry.id,
            usage=None,
            status=stream_status,
            event_id=usage_event_id,
            diagnostics=finish_diagnostics(diagnostics),
            request_content=request_content,
            request_metadata=websocket.state.request_metadata,
        )


@app.post("/v1/ocr/mineru")
async def legacy_mineru_ocr(request: Request):
    """Compatibility endpoint retained while MinerU is replaced by GLM-OCR."""
    payload = await _read_json(request)
    if payload.get("stream"):
        raise GatewayError(
            400,
            "The compatibility OCR endpoint does not support stream; use /v1/chat/completions for streaming",
            "unsupported_streaming",
            "invalid_request_error",
            "stream",
        )
    entry = REGISTRY.get("glm-ocr-0.9b", "chat")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GatewayError(400, "messages must be a non-empty array", "invalid_messages", "invalid_request_error", "messages")
    upstream_payload = dict(payload)
    await _admit(request, entry)
    upstream_payload["model"] = entry.upstream_model
    upstream_payload["messages"] = normalize_tool_history(messages)
    response = await _call_json(
        f"{entry.base_url}/v1/chat/completions",
        method="POST",
        payload=upstream_payload,
        headers=_upstream_headers(),
        timeout=CHAT_TIMEOUT,
        diagnostics=request.state.diagnostics,
    )
    response["model"] = entry.id
    await _record_usage(
        request,
        endpoint="/v1/ocr/mineru",
        model=entry.id,
        usage=response.get("usage"),
    )
    return response


# Last-added middleware is outermost; owns complete HTTP/SSE/WebSocket lifetime.
app.add_middleware(AdmissionLifetimeMiddleware)
