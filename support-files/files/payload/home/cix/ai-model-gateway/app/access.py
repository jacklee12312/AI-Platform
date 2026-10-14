"""Key policies and single-worker gateway admission, including streaming lifetime."""
from __future__ import annotations

import asyncio
import time
import threading
import uuid
import anyio
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import GatewayError
from .request_content import clean_request_content, clean_request_metadata, request_summary
from .request_diagnostics import clean_diagnostics

Limit = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000)]
ConcurrentLimit = Annotated[int, Field(strict=True, ge=0, le=1024)]
BUSINESS_TZ = timezone(timedelta(hours=8))


class KeyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    application: str = Field(default="", max_length=64)
    allowed_models: list[str] | None = Field(default=None, max_length=256)
    expires_at: str | None = None
    rate_limit_per_minute: Limit = 0  # 0 inherits the gateway rate limit.
    max_concurrent_requests: ConcurrentLimit = 0
    daily_request_limit: Limit = 0
    monthly_token_warning: Limit = 0  # Warning only: upstream usage may be unknown.

    @field_validator("application")
    @classmethod
    def clean_application(cls, value: str) -> str:
        return value.strip()

    @field_validator("allowed_models")
    @classmethod
    def clean_models(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if any(not item or item != item.strip() or len(item) > 128 for item in value):
            raise ValueError("模型 ID 必须为非空、无首尾空格的字符串")
        return sorted(set(value))

    @field_validator("expires_at")
    @classmethod
    def expiry(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None or date.utcoffset() is None:
                raise ValueError()
            return date.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        except (ValueError, OverflowError):
            raise ValueError("到期时间必须是带时区的 ISO 时间；清除到期时间请传 null") from None


class ResourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    global_max_concurrent: ConcurrentLimit = 0
    model_limits: dict[str, ConcurrentLimit] = Field(default_factory=dict, max_length=256)
    pool_limits: dict[str, ConcurrentLimit] = Field(default_factory=dict, max_length=3)

    @field_validator("pool_limits")
    @classmethod
    def pools(cls, value: dict[str, int]) -> dict[str, int]:
        if set(value) - {"LQ50", "CIX-NPU", "CPU"}:
            raise ValueError("未知设备组")
        return value


def expired(policy: dict[str, Any], now: datetime | None = None) -> bool:
    end = policy.get("expires_at")
    return bool(end and datetime.fromisoformat(end.replace("Z", "+00:00")) <= (now or datetime.now(timezone.utc)))


def authorize(policy: dict[str, Any], model: str) -> None:
    if expired(policy):
        raise GatewayError(401, "API Key 已到期，请联系管理员续期", "api_key_expired", "authentication_error")
    allowed = policy.get("allowed_models")
    if allowed is not None and model not in allowed:
        raise GatewayError(403, f"该 API Key 未获授权调用模型 {model}", "model_not_allowed", "permission_error", "model")


def model_pools(metadata: dict[str, Any]) -> list[str]:
    device = str(metadata.get("device", ""))
    return [pool for pool in ("LQ50", "CIX-NPU", "CPU") if pool in device]


class Lease:
    def __init__(
        self,
        controller: "AdmissionController",
        dimensions: list[tuple[str, str]],
        in_flight: dict[tuple[str, str], int] | None = None,
        limits: dict[tuple[str, str], int] | None = None,
        metadata: dict | None = None,
    ):
        self.controller, self.dimensions = controller, dimensions
        self.in_flight = in_flight or {}
        self.limits = limits or {}
        self.finished = False
        self.workers = 0
        self.released = False
        self.metadata = metadata or {}
        self.id = self.metadata.get("id") or uuid.uuid4().hex
        self.started = time.perf_counter()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.cancel_requested = False
        self.cancel_event = asyncio.Event()
        self.cancel_supported = bool(self.metadata.get("cancel_supported"))

    def _release_if_done(self) -> None:
        if self.finished and not self.workers and not self.released:
            self.released = True
            self.controller.leases.pop(self.id, None)
            for dimension in self.dimensions:
                self.controller.active[dimension] -= 1
                if not self.controller.active[dimension]:
                    del self.controller.active[dimension]

    def finish(self) -> None:
        with self.controller.lock:
            self.finished = True
            self._release_if_done()

    def start_work(self) -> None:
        with self.controller.lock:
            self.workers += 1

    def end_work(self) -> None:
        with self.controller.lock:
            self.workers -= 1
            self._release_if_done()


class AdmissionController:
    def __init__(self):
        self.lock = threading.Lock()
        self.active: Counter[tuple[str, str]] = Counter()
        self.leases: dict[str, Lease] = {}

    def acquire(
        self,
        key_id: str,
        model: str,
        pools: list[str],
        key_policy: dict,
        resources: dict,
        metadata: dict | None = None,
    ) -> Lease:
        dimensions = [("global", "all"), ("key", key_id), ("model", model), *[("pool", p) for p in pools]]
        limits = [resources.get("global_max_concurrent", 0), key_policy.get("max_concurrent_requests", 0),
                  resources.get("model_limits", {}).get(model, 0),
                  *[resources.get("pool_limits", {}).get(p, 0) for p in pools]]
        with self.lock:
            for dimension, limit in zip(dimensions, limits):
                if limit and self.active[dimension] >= limit:
                    level = dimension[0]
                    labels = {"global": "网关总并发", "key": "该 Key 并发", "model": "模型并发", "pool": "设备组并发"}
                    raise GatewayError(429, f"{labels[level]}已达上限，请等待在途请求结束后重试",
                                       f"{level}_concurrency_exceeded", "rate_limit_error",
                                       details={"scope": level, "target": dimension[1] if level != "key" else "self",
                                                "limit": limit, "in_flight": self.active[dimension], "retry_after": 1})
            self.active.update(dimensions)
            in_flight = {dimension: self.active[dimension] for dimension in dimensions}
            configured = dict(zip(dimensions, limits))
            lease = Lease(self, dimensions, in_flight, configured, metadata)
            self.leases[lease.id] = lease
        return lease

    def cancel(self, event_id: str) -> dict | None:
        """Signal one internal request while retaining its slot until cleanup finishes."""
        with self.lock:
            lease = self.leases.get(event_id)
            if lease is None or lease.finished:
                return None
            if not lease.cancel_supported:
                raise GatewayError(
                    409,
                    "此类请求暂不支持安全中断，不会重启模型或强制清空名额",
                    "cancellation_not_supported",
                )
            repeated = lease.cancel_requested
            lease.cancel_requested = True
            lease.cancel_event.set()
            return {
                "id": event_id,
                "status": "cancelling",
                "already_requested": repeated,
                "message": "已发送中断信号，等待上游连接关闭后释放名额",
            }

    def _detail(self, lease: Lease, include_content: bool = False) -> dict:
        state = lease.metadata.get("state", {})
        raw_diagnostics = state.get("diagnostics", {})
        diagnostics = clean_diagnostics(raw_diagnostics)
        diagnostics.update(
            elapsed_ms=round(
                (time.perf_counter() - raw_diagnostics.get("_started", lease.started))
                * 1000,
                3,
            ),
            started_at=diagnostics.get("started_at", lease.started_at),
        )
        content = state.get("request_content", {})
        model = next(name for kind, name in lease.dimensions if kind == "model")
        key = next(name for kind, name in lease.dimensions if kind == "key")
        result = {
            "id": lease.id,
            "request_id": state.get("request_id", lease.id),
            "model": model,
            "api_key_id": key,
            "key_name": lease.metadata.get("key_name", key),
            "prefix": "",
            "endpoint": lease.metadata.get("endpoint", ""),
            "created_at": lease.started_at,
            "status": "in_flight",
            "phase": (
                "finishing"
                if lease.finished
                else "cancelling"
                if lease.cancel_requested
                else "active"
            ),
            "cancel_supported": lease.cancel_supported and not lease.finished,
            "cancel_requested": lease.cancel_requested,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "usage_reported": False,
            "request_metadata": clean_request_metadata(state.get("request_metadata")),
            "request_summary": request_summary(content),
            "diagnostics": diagnostics,
        }
        if include_content:
            result["request_content"] = clean_request_content(content)
        return result

    def request_snapshot(self, limit: int = 200) -> dict:
        with self.lock:
            counts = {
                "global": self.active.get(("global", "all"), 0),
                **{
                    kind: {
                        name: count
                        for (level, name), count in self.active.items()
                        if level == kind
                    }
                    for kind in ("key", "model", "pool")
                },
            }
            leases = list(self.leases.values())
            return {
                "in_flight": counts,
                "requests": [self._detail(lease) for lease in leases[:limit]],
                "total": len(leases),
                "truncated": len(leases) > limit,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "worker_scope": "single-process",
            }

    def request_detail(self, event_id: str) -> dict | None:
        with self.lock:
            lease = self.leases.get(event_id)
            return self._detail(lease, include_content=True) if lease else None

    def snapshot(self) -> dict:
        with self.lock:
            return {"global": self.active.get(("global", "all"), 0),
                    **{kind: {name: count for (level, name), count in self.active.items() if level == kind}
                       for kind in ("key", "model", "pool")}}


_scope: ContextVar[dict | None] = ContextVar("resource_scope", default=None)


def current_lease() -> Lease | None:
    scope = _scope.get()
    return scope.get("admission_lease") if scope is not None else None


async def cancellable(awaitable, lease: Lease | None = None):
    """Cancel async upstream I/O and wait for cleanup before releasing the lease."""
    lease = lease or current_lease()
    if lease is None or not lease.cancel_supported:
        return await awaitable
    worker = asyncio.ensure_future(awaitable)
    signal = asyncio.create_task(lease.cancel_event.wait())
    lease.start_work()
    try:
        if lease.cancel_requested:
            raise GatewayError(
                499,
                "此请求已由管理员中断",
                "admin_cancelled",
                "request_cancelled",
            )
        done, _ = await asyncio.wait(
            (worker, signal), return_when=asyncio.FIRST_COMPLETED
        )
        if worker in done:
            return await worker
        worker.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(worker, return_exceptions=True)
        raise GatewayError(
            499,
            "此请求已由管理员中断",
            "admin_cancelled",
            "request_cancelled",
        )
    finally:
        signal.cancel()
        if not worker.done():
            worker.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(signal, worker, return_exceptions=True)
        lease.end_work()


class AdmissionLifetimeMiddleware:
    """Outer ASGI layer: do not release at response headers or the first SSE event."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = _scope.set(scope)
        try:
            await self.app(scope, receive, send)
        finally:
            lease = scope.get("admission_lease")
            if lease is not None:
                lease.finish()
            _scope.reset(token)


async def upstream_thread(function, *args, **kwargs):
    """A disconnected HTTP client cannot release a still-running urllib worker."""
    scope = _scope.get()
    lease = scope.get("admission_lease") if scope is not None else None
    if lease is None:
        return await asyncio.to_thread(function, *args, **kwargs)
    lease.start_work()
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))

    def done(completed):
        lease.end_work()
        if not completed.cancelled():
            completed.exception()  # Retrieve exceptions even if the caller disconnected.

    task.add_done_callback(done)
    return await asyncio.shield(task)
