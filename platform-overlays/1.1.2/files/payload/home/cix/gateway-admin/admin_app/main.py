from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal
from urllib.parse import urlencode

import httpx
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .audit import AuditStore, JOB_ID
from .batch_store import BatchStore
from .batch_engine import BatchEngine
from .batch_api import router as batch_router
from .capabilities import build_catalog, export_markdown
from .api_reference import build_openapi, build_reference
from .diagnostics import DiagnosticsStore, audit_history


VERSION = "0.7.0"
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://ai-model-gateway:17700").rstrip("/")
GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY") or os.getenv("HOUMO_API_KEY", "")
GATEWAY_MANAGEMENT_TOKEN = os.getenv("GATEWAY_MANAGEMENT_TOKEN", "")
AGENT_URL = os.getenv("ADMIN_AGENT_URL", "http://gateway-admin-agent:17781").rstrip("/")
AGENT_TOKEN = os.getenv("ADMIN_AGENT_TOKEN", "")
PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
COOKIE_SECURE = os.getenv("ADMIN_COOKIE_SECURE", "false").lower() == "true"
SESSION_TTL = int(os.getenv("ADMIN_SESSION_TTL_SECONDS", "28800"))
DATA_DIR = Path(os.getenv("ADMIN_DATA_DIR", "/data"))
STATIC_DIR = Path(os.getenv("ADMIN_STATIC_DIR", "/app/static"))
DB_PATH = DATA_DIR / "modeldeck.db"
SESSION_COOKIE = "modeldeck_session"
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
AUDIT_POLL_SECONDS = 3
LOGGER = logging.getLogger(__name__)
BATCH_ENGINE: BatchEngine | None = None
DEVICE_MODES = frozenset({"copaw", "spark-x1", "qwen38", "qwen36", "qwen-vl", "glm-ocr", "bge-m3", "reranker", "cosyvoice"})

@asynccontextmanager
async def lifespan(_: FastAPI):
    global BATCH_ENGINE
    await asyncio.to_thread(_initialize_db)
    BATCH_ENGINE = BatchEngine(BatchStore(DATA_DIR), GATEWAY_URL, _gateway_headers, _audit)
    await BATCH_ENGINE.start()
    tracker = asyncio.create_task(_track_audit_jobs(), name="audit-job-tracker")
    try:
        yield
    finally:
        await BATCH_ENGINE.stop()
        tracker.cancel()
        with suppress(asyncio.CancelledError):
            await tracker


app = FastAPI(title="ModelDeck", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
SESSIONS: dict[str, dict[str, Any]] = {}
SESSION_LOCK = threading.Lock()
LOGIN_ATTEMPTS: dict[str, deque[float]] = defaultdict(deque)
LOGIN_LOCK = threading.Lock()


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class ModeRequest(BaseModel):
    mode: str


class RecoveryRequest(BaseModel):
    mode: str
    confirmation: Literal["恢复设备"]


class ChatTestRequest(BaseModel):
    model: str
    prompt: str = Field(min_length=1, max_length=32_000)
    stream: bool = True
    max_tokens: int = Field(default=128, ge=1, le=2048)
    temperature: float = Field(default=0.2, ge=0, le=2)
    image_url: str | None = Field(default=None, max_length=20_000_000)


class ConfigModel(BaseModel):
    id: str
    enabled: bool
    base_url: str


class ConfigRequest(BaseModel):
    settings: dict[str, Any]
    models: list[ConfigModel] = Field(min_length=1)


class APIKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    policy: dict[str, Any] = Field(default_factory=dict)


class APIKeyUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None
    policy: dict[str, Any] | None = None


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    with _audit_store().connection() as connection:
        yield connection


def _audit_store() -> AuditStore:
    return AuditStore(DB_PATH, (GATEWAY_API_KEY, GATEWAY_MANAGEMENT_TOKEN, AGENT_TOKEN, PASSWORD_HASH))


def _initialize_db() -> None:
    _audit_store().initialize()


def _audit(action: str, target: str, status: str, request_id: str, detail: dict[str, Any] | str = "", *, job_id: str = "", actor: str = "admin") -> None:
    _audit_store().write(action, target, status, request_id, detail, job_id=job_id, actor=actor)


async def _audit_request(request: Request, action: str, target: str, status: str,
                         detail: Any = "", *, job_id: str = "") -> None:
    authenticated = _session(request.cookies.get(SESSION_COOKIE)) is not None
    actor = "admin" if authenticated or (action in {"login", "logout"} and status == "succeeded") else "anonymous"
    await asyncio.to_thread(_audit, action, target, status, request.state.request_id, detail, job_id=job_id, actor=actor)
    request.state.audit_recorded = True


class AgentRequestError(HTTPException):
    def __init__(self, upstream_status: int, detail: Any):
        # Preserve an explicit operation denial. Agent credential failures are
        # infrastructure errors, not a reason to log the UI administrator out.
        super().__init__(status_code=upstream_status if upstream_status in {400, 404, 409, 422, 429} else 502, detail=detail)
        self.upstream_status = upstream_status


async def _poll_audit_job(job_id: str) -> None:
    try:
        snapshot = await _agent("GET", f"/v1/jobs/{job_id}", timeout=10)
    except AgentRequestError as exc:
        if exc.upstream_status != 404:
            raise
        # The restricted agent currently keeps jobs in memory. A lost job is not
        # proof of either success or failure, nor permission to execute it again.
        snapshot = {"id": job_id, "status": "unknown", "error": "代理已无此任务，最终结果无法确认；请核查服务状态。"}
    if snapshot.get("id") != job_id:
        raise ValueError("agent returned a different job id")
    await asyncio.to_thread(_audit_store().record_job, snapshot)


async def _reconcile_audit_jobs() -> None:
    job_ids = await asyncio.to_thread(_audit_store().pending)
    semaphore = asyncio.Semaphore(4)

    async def check(job_id: str) -> None:
        async with semaphore:
            try:
                await _poll_audit_job(job_id)
            except Exception as exc:
                # Retry transient outages next cycle; do not fabricate a failed
                # operation or print unredacted upstream exception bodies.
                LOGGER.warning("Audit job reconciliation deferred (%s)", type(exc).__name__)

    await asyncio.gather(*(check(job_id) for job_id in job_ids))


async def _track_audit_jobs() -> None:
    while True:
        try:
            await _reconcile_audit_jobs()
        except Exception as exc:
            LOGGER.error("Audit reconciliation unavailable (%s)", type(exc).__name__)
        await asyncio.sleep(AUDIT_POLL_SECONDS)


async def _submit_audited_job(request: Request, action: str, target: str, path: str,
                              *, json_body: Any = None, timeout: float = 15) -> Any:
    try:
        result = await _agent("POST", path, json_body=json_body, timeout=timeout)
    except Exception as exc:
        # A transport timeout may occur after the operation was accepted. Only
        # an explicit agent rejection proves that it did not run.
        status = "failed" if isinstance(exc, AgentRequestError) and exc.upstream_status < 500 else "unknown"
        await _audit_request(request, action, target, status, {
            "error": exc.detail if isinstance(exc, HTTPException) else type(exc).__name__,
        })
        raise
    job_id = result.get("id", "") if isinstance(result, dict) else ""
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id) or result.get("status") not in {"queued", "running", "succeeded", "failed"}:
        await _audit_request(request, action, target, "unknown", {"error": "代理返回的任务ID无效，无法追踪执行结果"})
        raise HTTPException(status_code=502, detail="invalid agent job response")
    await _audit_request(request, action, target, "accepted", {"job_id": job_id}, job_id=job_id)
    await asyncio.to_thread(_audit_store().record_job, result)
    return result


def _gateway_headers() -> dict[str, str]:
    if GATEWAY_MANAGEMENT_TOKEN:
        return {"X-Gateway-Admin-Token": GATEWAY_MANAGEMENT_TOKEN}
    return {"Authorization": f"Bearer {GATEWAY_API_KEY}"} if GATEWAY_API_KEY else {}


def _agent_headers() -> dict[str, str]:
    return {"X-Agent-Token": AGENT_TOKEN}


def _password_valid(password: str) -> bool:
    try:
        algorithm, iterations, salt_value, digest_value = PASSWORD_HASH.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_value.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_value.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _prune_sessions() -> None:
    now = time.time()
    for token in [token for token, session in SESSIONS.items() if session["expires_at"] <= now]:
        SESSIONS.pop(token, None)


def _session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    with SESSION_LOCK:
        _prune_sessions()
        value = SESSIONS.get(token)
        if value is not None:
            value["expires_at"] = time.time() + SESSION_TTL
        return value


def require_session(modeldeck_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    value = _session(modeldeck_session)
    if value is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return value


def require_csrf(
    session: dict[str, Any] = Depends(require_session),
    x_csrf_token: str = Header(default=""),
) -> dict[str, Any]:
    if not x_csrf_token or not secrets.compare_digest(x_csrf_token, session["csrf"]):
        raise HTTPException(status_code=403, detail="invalid csrf token")
    return session


async def _agent(method: str, path: str, *, json_body: Any = None, timeout: float = 30) -> Any:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, f"{AGENT_URL}{path}", headers=_agent_headers(), json=json_body)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="restricted operations agent is unavailable") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": response.text[:1000]}
    if response.status_code >= 400:
        raise AgentRequestError(response.status_code, payload.get("detail", "agent request failed"))
    return payload


async def _gateway(method: str, path: str, *, json_body: Any = None, timeout: float = 30, request_id: str | None = None) -> Any:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            headers = _gateway_headers()
            if request_id:
                headers['X-Request-ID'] = request_id
            response = await client.request(method, f"{GATEWAY_URL}{path}", headers=headers, json=json_body)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="model gateway is unavailable") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": response.text[:1000]}
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=payload)
    return payload


@app.middleware("http")
async def security_headers(request: Request, call_next):
    supplied_id = request.headers.get("X-Request-ID", "")
    request_id = supplied_id if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", supplied_id) else uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    if response.status_code >= 400 and not getattr(request.state, "audit_recorded", False):
        target = request.url.path
        action = {
            "/api/login": "login", "/api/logout": "logout", "/api/mode/switch": "mode-switch",
            "/api/config/apply": "config-apply", "/api/api-keys": "api-key-create",
            "/api/resource-policy": "resource-policy-update",
            "/api/device/recover": "device-recovery",
        }.get(target)
        if target.startswith("/api/services/") and target.endswith("/restart"):
            action = "restart"
        elif target.startswith("/api/api-keys/"):
            action = "api-key-revoke" if request.method == "DELETE" else "api-key-update"
        elif target.startswith("/api/batches"):
            action = "batch-operation"
        elif target.startswith('/api/model-install'):
            action = 'model-operation'
        elif target.startswith('/api/evaluations'):
            action = 'evaluation-operation'
        elif target.startswith('/api/test/'):
            action = 'api-test'
        if action and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            # Authentication/CSRF/schema failures never enter the route handler.
            # Log only the rejection code, not passwords, cookies or request bodies.
            await _audit_request(request, action, target, "failed", {"http_status": response.status_code})
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health/live")
async def live() -> dict[str, Any]:
    return {
        "ok": bool(PASSWORD_HASH and GATEWAY_MANAGEMENT_TOKEN and AGENT_TOKEN),
        "service": "modeldeck-admin",
        "version": VERSION,
    }


@app.post("/api/login")
async def login(request: Request, payload: LoginRequest) -> Response:
    address = request.client.host if request.client else "unknown"
    now = time.time()
    with LOGIN_LOCK:
        attempts = LOGIN_ATTEMPTS[address]
        while attempts and attempts[0] < now - 600:
            attempts.popleft()
        if len(attempts) >= 8:
            raise HTTPException(status_code=429, detail="too many login attempts")
        attempts.append(now)
    if not PASSWORD_HASH:
        raise HTTPException(status_code=503, detail="administrator password is not configured")
    if not _password_valid(payload.password):
        await _audit_request(request, "login", address, "failed", {"error": "invalid password"})
        raise HTTPException(status_code=401, detail="invalid password")

    token = secrets.token_urlsafe(32)
    session = {"csrf": secrets.token_urlsafe(24), "expires_at": now + SESSION_TTL, "username": "admin"}
    with SESSION_LOCK:
        SESSIONS[token] = session
    with LOGIN_LOCK:
        LOGIN_ATTEMPTS.pop(address, None)
    await _audit_request(request, "login", address, "succeeded")
    response = JSONResponse({"authenticated": True, "username": "admin", "csrf": session["csrf"]})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        max_age=SESSION_TTL,
        path="/",
    )
    return response


@app.post("/api/logout")
async def logout(
    request: Request,
    modeldeck_session: str | None = Cookie(default=None),
    _: dict[str, Any] = Depends(require_csrf),
) -> Response:
    if modeldeck_session:
        with SESSION_LOCK:
            SESSIONS.pop(modeldeck_session, None)
    await _audit_request(request, "logout", "session", "succeeded")
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/session")
async def session_info(modeldeck_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    session = _session(modeldeck_session)
    if session is None:
        return {"authenticated": False}
    return {"authenticated": True, "username": session["username"], "csrf": session["csrf"]}


@app.get("/api/overview")
async def overview(_: dict[str, Any] = Depends(require_session)) -> dict[str, Any]:
    async def safe_agent() -> dict[str, Any]:
        try:
            return await _agent("GET", "/v1/status", timeout=45)
        except Exception as exc:  # noqa: BLE001 - represent partial control-plane outage
            return {"error": str(exc), "system": {}, "containers": [], "modelctl": {}}

    gateway, agent = await asyncio.gather(_gateway("GET", "/health", timeout=15), safe_agent())
    return {"gateway": gateway, "host": agent, "refreshed_at": _now()}


@app.get("/api/models")
async def models(_: dict[str, Any] = Depends(require_session)) -> Any:
    return await _gateway("GET", "/v1/models", timeout=15)


def _public_gateway_url(request: Request) -> str:
    hostname = request.url.hostname or ""
    if not re.fullmatch(r"[A-Za-z0-9.-]+", hostname):
        return "http://192.168.133.1:17700"
    return f"{'https' if request.url.scheme == 'https' else 'http'}://{hostname}:17700"


async def _capability_snapshot(base_url: str | None = None):
    values = await asyncio.gather(_gateway('GET', '/v1/models', timeout=15),
                                  _agent('GET', '/v1/config', timeout=15), return_exceptions=True)
    registry_error = not isinstance(values[0], dict) or not isinstance(values[0].get('data'), list)
    config_error = not isinstance(values[1], dict) or not isinstance(values[1].get('settings'), dict)
    return build_catalog(None if registry_error else values[0], None if config_error else values[1],
                         registry_error=registry_error, config_error=config_error,
                         base_url=base_url or "http://192.168.133.1:17700")


@app.get('/api/model-capabilities')
async def model_capabilities(request: Request, _: dict[str, Any] = Depends(require_session)):
    return await _capability_snapshot(_public_gateway_url(request))


@app.get('/api/model-capabilities/export.md')
async def model_capabilities_export(request: Request, _: dict[str, Any] = Depends(require_session)):
    return Response(export_markdown(await _capability_snapshot(_public_gateway_url(request))), media_type='text/markdown; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="model-capabilities.md"'})


async def _api_reference_snapshot(base_url: str | None = None):
    values = await asyncio.gather(_gateway('GET', '/v1/models', timeout=15),
                                  _agent('GET', '/v1/config', timeout=15), return_exceptions=True)
    registry_error = not isinstance(values[0], dict) or not isinstance(values[0].get('data'), list)
    config_error = not isinstance(values[1], dict) or not isinstance(values[1].get('settings'), dict)
    return build_reference(None if registry_error else values[0], None if config_error else values[1],
                           registry_error=registry_error, config_error=config_error,
                           base_url=base_url or "http://192.168.133.1:17700")


@app.get('/api/api-reference')
async def api_reference(request: Request, _: dict[str, Any] = Depends(require_session)):
    return await _api_reference_snapshot(_public_gateway_url(request))


@app.get('/api/api-reference/openapi.json')
async def api_reference_openapi(request: Request, _: dict[str, Any] = Depends(require_session)):
    document = build_openapi(await _api_reference_snapshot(_public_gateway_url(request)))
    return Response(json.dumps(document, ensure_ascii=False, indent=2), media_type='application/json',
                    headers={'Content-Disposition': 'attachment; filename="modeldeck-openapi.json"'})


async def _diagnostics_snapshot():
    store = DiagnosticsStore(DATA_DIR / 'diagnostics.db')
    values = await asyncio.gather(_agent('GET', '/v1/diagnostics', timeout=45),
                                  asyncio.to_thread(audit_history, _audit_store()), return_exceptions=True)
    snapshot = values[0] if isinstance(values[0], dict) and isinstance(values[0].get('containers'), list) else None
    if snapshot is not None: await asyncio.to_thread(store.observe, snapshot)
    return {'agent_available': snapshot is not None, 'current': snapshot, 'history': await asyncio.to_thread(store.history),
            'operations': values[1] if isinstance(values[1], list) else [], 'audit_available': isinstance(values[1], list),
            'notice': '实际参数、启动声明、保存配置与版本基线分别标注；配置差异只供核对，不自动更改运行状态。',
            'history_notice': '部署时间来自 Docker 容器创建时间；重启另行记录。只保存本页采集到的变更，首次观测不代表完整历史发布记录。最近 100 条可见，总计保留 1000 条事件。',
            'fault_notice': '故障线索来自近 24 小时每容器末尾 200 行的有限扫描、容器状态、已有恢复证据和管理审计；没有记录不等于从未发生故障。本页按需采集，未建立持续监控。'}


@app.get('/api/diagnostics')
async def diagnostics(_: dict[str, Any] = Depends(require_session)):
    return await _diagnostics_snapshot()


@app.get('/api/diagnostics/export.json')
async def diagnostics_export(_: dict[str, Any] = Depends(require_session)):
    return Response(json.dumps(await _diagnostics_snapshot(), ensure_ascii=False, indent=2), media_type='application/json',
                    headers={'Content-Disposition': 'attachment; filename="modeldeck-diagnostics.json"'})


@app.post("/api/mode/switch")
async def switch_mode(request: Request, payload: ModeRequest, _: dict[str, Any] = Depends(require_csrf)) -> Any:
    if payload.mode not in DEVICE_MODES:
        raise HTTPException(status_code=400, detail="invalid mode")
    return await _submit_audited_job(request, "mode-switch", payload.mode, "/v1/mode-switch", json_body=payload.model_dump())


@app.post("/api/services/{service}/restart")
async def restart(request: Request, service: str, _: dict[str, Any] = Depends(require_csrf)) -> Any:
    return await _submit_audited_job(request, "restart", service, f"/v1/restart/{service}")


@app.post("/api/device/recover")
async def recover_device(request: Request, payload: RecoveryRequest, _: dict[str, Any] = Depends(require_csrf)) -> Any:
    if payload.mode not in DEVICE_MODES:
        raise HTTPException(status_code=400, detail="invalid mode")
    return await _submit_audited_job(request, "device-recovery", "houmo-xh2", "/v1/device-recovery", json_body=payload.model_dump())


@app.get("/api/jobs/{job_id}")
async def job(job_id: str, _: dict[str, Any] = Depends(require_session)) -> Any:
    if not JOB_ID.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="invalid job id")
    try:
        result = await _agent("GET", f"/v1/jobs/{job_id}", timeout=15)
    except HTTPException:
        rows = await asyncio.to_thread(_audit_store().read, job_id=job_id, limit=1)
        if rows and rows[0]["status"] in {"succeeded", "failed", "unknown"}:
            detail = json.loads(rows[0]["detail"])
            return {"id": job_id, "status": rows[0]["status"], "kind": rows[0]["action"],
                    "target": rows[0]["target"], "created_at": rows[0]["created_at"],
                    **detail.get("job", {}), "from_audit": True}
        raise
    if result.get("id") != job_id:
        raise HTTPException(status_code=502, detail="invalid agent job response")
    await asyncio.to_thread(_audit_store().record_job, result)
    return result


@app.get("/api/logs")
async def logs(service: str, tail: int = 200, _: dict[str, Any] = Depends(require_session)) -> Any:
    if not (20 <= tail <= 1000):
        raise HTTPException(status_code=400, detail="tail must be between 20 and 1000")
    return await _agent("GET", f"/v1/logs?service={service}&tail={tail}", timeout=35)


@app.get("/api/audit")
async def audit(limit: int = 100, request_id: str = Query(default="", max_length=128),
                job_id: str = Query(default="", max_length=128), q: str = Query(default="", max_length=200),
                _: dict[str, Any] = Depends(require_session)) -> dict[str, Any]:
    return {"data": await asyncio.to_thread(_audit_store().read, limit=limit, request_id=request_id, job_id=job_id, q=q)}


@app.get("/api/api-keys")
async def api_keys(_: dict[str, Any] = Depends(require_session)) -> Any:
    return await _gateway("GET", "/internal/api-keys", timeout=15)


@app.get("/api/resource-policy")
async def resource_policy(_: dict[str, Any] = Depends(require_session)) -> Any:
    return await _gateway("GET", "/internal/resource-policy", timeout=15)


@app.get("/api/requests/active")
async def active_requests(_: dict[str, Any] = Depends(require_session)) -> Any:
    return JSONResponse(
        await _gateway("GET", "/internal/requests/active", timeout=15),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/requests/{event_id}")
async def request_detail(
    event_id: str, _: dict[str, Any] = Depends(require_session)
) -> Any:
    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise HTTPException(status_code=400, detail="无效的内部请求 ID")
    return JSONResponse(
        await _gateway("GET", f"/internal/requests/{event_id}", timeout=15),
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/requests/{event_id}/cancel")
async def cancel_request(
    event_id: str,
    request: Request,
    _: dict[str, Any] = Depends(require_csrf),
) -> Any:
    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise HTTPException(status_code=400, detail="无效的内部请求 ID")
    result = await _gateway(
        "POST", f"/internal/requests/{event_id}/cancel", timeout=15
    )
    await _audit_request(
        request,
        "request-cancel",
        event_id,
        result["status"],
        {"event_id": event_id},
    )
    return JSONResponse(
        result,
        status_code=202 if result["status"] == "cancelling" else 200,
        headers={"Cache-Control": "no-store"},
    )


@app.put("/api/resource-policy")
async def update_resource_policy(request: Request, payload: dict[str, Any],
                                  _: dict[str, Any] = Depends(require_csrf)) -> Any:
    result = await _gateway("PUT", "/internal/resource-policy", json_body=payload, timeout=15)
    await _audit_request(request, "resource-policy-update", "gateway", "succeeded", {"policy": result["policy"]})
    return result


@app.post("/api/api-keys", status_code=201)
async def create_api_key(
    request: Request,
    payload: APIKeyCreateRequest,
    _: dict[str, Any] = Depends(require_csrf),
) -> Any:
    result = await _gateway("POST", "/internal/api-keys", json_body=payload.model_dump(), timeout=15)
    await _audit_request(
        request,
        "api-key-create",
        result["id"],
        "succeeded",
        {"name": result["name"], "prefix": result["prefix"], "policy": result.get("policy", {})},
    )
    return result


@app.patch("/api/api-keys/{key_id}")
async def update_api_key(
    request: Request,
    key_id: str,
    payload: APIKeyUpdateRequest,
    _: dict[str, Any] = Depends(require_csrf),
) -> Any:
    result = await _gateway("PATCH", f"/internal/api-keys/{key_id}", json_body=payload.model_dump(exclude_none=True), timeout=15)
    await _audit_request(
        request,
        "api-key-update",
        key_id,
        "succeeded",
        {"name": payload.name, "enabled": payload.enabled, "policy": payload.policy},
    )
    return result


@app.delete("/api/api-keys/{key_id}")
async def revoke_api_key(
    request: Request,
    key_id: str,
    _: dict[str, Any] = Depends(require_csrf),
) -> Any:
    result = await _gateway("DELETE", f"/internal/api-keys/{key_id}", timeout=15)
    await _audit_request(
        request,
        "api-key-revoke",
        key_id,
        "succeeded",
        {"prefix": result["prefix"]},
    )
    return result


@app.get("/api/usage")
async def token_usage(
    api_key_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
    offset: int = Query(default=0, ge=0, le=1_000_000),
    model: str | None = Query(default=None, max_length=128),
    request_id: str | None = Query(default=None, max_length=128),
    status: str | None = Query(default=None, max_length=128),
    error_code: str | None = Query(default=None, max_length=64),
    _: dict[str, Any] = Depends(require_session),
) -> Any:
    limit = max(1, min(limit, 500))
    parameters: dict[str, Any] = {"limit": limit, "offset": offset}
    for name, value in (("model", model), ("request_id", request_id), ("status", status), ("error_code", error_code)):
        if value:
            parameters[name] = value
    if api_key_id:
        parameters["api_key_id"] = api_key_id
    if start:
        parameters["start"] = start
    if end:
        parameters["end"] = end
    return await _gateway("GET", f"/internal/usage?{urlencode(parameters)}", timeout=20)


@app.get("/api/usage/export")
async def export_usage(
    api_key_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    model: str | None = Query(default=None, max_length=128),
    request_id: str | None = Query(default=None, max_length=128),
    status: str | None = Query(default=None, max_length=128),
    error_code: str | None = Query(default=None, max_length=64),
    _: dict[str, Any] = Depends(require_session),
) -> Response:
    parameters = {
        key: value
        for key, value in {
            "api_key_id": api_key_id,
            "start": start,
            "end": end,
            "model": model,
            "request_id": request_id,
            "status": status,
            "error_code": error_code,
        }.items()
        if value
    }
    result = await _gateway(
        "GET", f"/internal/usage/export?{urlencode(parameters)}", timeout=60
    )
    return Response(
        result["csv"],
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="modeldeck-usage.csv"',
            "Cache-Control": "no-store",
            "X-Record-Count": str(result["record_count"]),
        },
    )


@app.get("/api/config")
async def config(_: dict[str, Any] = Depends(require_session)) -> Any:
    return await _agent("GET", "/v1/config", timeout=20)


@app.post("/api/config/validate")
async def validate_config(payload: ConfigRequest, _: dict[str, Any] = Depends(require_session)) -> Any:
    return await _agent("POST", "/v1/config/validate", json_body=payload.model_dump(), timeout=20)


@app.post("/api/config/apply")
async def apply_config(request: Request, payload: ConfigRequest, _: dict[str, Any] = Depends(require_csrf)) -> Any:
    return await _submit_audited_job(request, "config-apply", "runtime-config", "/v1/config/apply",
                                     json_body=payload.model_dump(), timeout=20)


@app.post("/api/test/chat")
async def test_chat(request: Request, payload: ChatTestRequest, _: dict[str, Any] = Depends(require_csrf)) -> Response:
    if not MODEL_ID_RE.match(payload.model):
        raise HTTPException(status_code=400, detail="invalid model id")
    message_content: str | list[dict[str, Any]] = payload.prompt
    if payload.image_url:
        from .experience_api import MAX_IMAGE, image_type
        if not re.fullmatch(r'data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+', payload.image_url):
            raise HTTPException(status_code=400, detail="image_url must be a PNG, JPEG or WebP data URL")
        try:
            raw_image = base64.b64decode(payload.image_url.split(',', 1)[1], validate=True)
        except ValueError:
            raise HTTPException(422, '图片编码无效') from None
        if len(raw_image) > MAX_IMAGE:
            raise HTTPException(413, '图片不能超过 12 MB')
        await asyncio.to_thread(image_type, raw_image)
        message_content = [
            {"type": "image_url", "image_url": {"url": payload.image_url}},
            {"type": "text", "text": payload.prompt},
        ]
    gateway_payload = {
        "model": payload.model,
        "messages": [{"role": "user", "content": message_content}],
        "stream": payload.stream,
        "max_tokens": payload.max_tokens,
        "temperature": payload.temperature,
    }
    await asyncio.to_thread(
        _audit,
        "api-test",
        payload.model,
        "started",
        request.state.request_id,
        {"stream": payload.stream, "max_tokens": payload.max_tokens, "image": bool(payload.image_url)},
    )
    if not payload.stream:
        result = await _gateway("POST", "/v1/chat/completions", json_body=gateway_payload, timeout=360, request_id=request.state.request_id)
        return JSONResponse(result)

    return await _stream_chat(request, gateway_payload)


async def _stream_chat(request: Request, gateway_payload: dict) -> Response:

    client = httpx.AsyncClient(timeout=httpx.Timeout(360, connect=10))
    upstream_request = client.build_request(
        "POST",
        f"{GATEWAY_URL}/v1/chat/completions",
        headers={**_gateway_headers(), "Accept": "text/event-stream", "X-Request-ID": request.state.request_id},
        json=gateway_payload,
    )
    upstream = await client.send(upstream_request, stream=True)
    if upstream.status_code >= 400:
        body = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        media_type = upstream.headers.get("content-type", "application/json").split(";", 1)[0]
        return Response(body, status_code=upstream.status_code, media_type=media_type)

    async def events():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {
        "X-Guard-Input": upstream.headers.get("X-Guard-Input", ""),
        "X-Guard-Output": upstream.headers.get("X-Guard-Output", "Streaming"),
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)


def _batch_engine() -> BatchEngine:
    if BATCH_ENGINE is None:
        raise HTTPException(503, '批量任务服务正在启动')
    return BATCH_ENGINE


app.include_router(batch_router(_batch_engine, require_session, require_csrf, _audit_request))

import sys
from .model_install_api import router as model_install_router
app.include_router(model_install_router(sys.modules[__name__]))
from .evaluation_api import router as evaluation_router
app.include_router(evaluation_router(sys.modules[__name__]))
from .experience_api import router as experience_router, forward as forward_experience

async def _experience_proxy(request, path, **kwargs):
    return await forward_experience(sys.modules[__name__], request, path, **kwargs)

app.include_router(experience_router(sys.modules[__name__]))


@app.get("/{path:path}", include_in_schema=False)
async def spa(path: str) -> Response:
    candidate = (STATIC_DIR / path).resolve()
    try:
        candidate.relative_to(STATIC_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=404)
    if path and candidate.is_file():
        return FileResponse(candidate, headers={"Cache-Control": "no-cache"})
    index = STATIC_DIR / "index.html"
    if index.is_file():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return JSONResponse(status_code=503, content={"detail": "frontend assets are not built"})
