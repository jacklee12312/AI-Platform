"""Persist bounded request diagnostics and administrator-visible request content."""
from __future__ import annotations

import json
import math
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from datetime import datetime, timezone


REASONS = {
    "admin_cancelled": ("管理员中断", "管理员主动中断了本次请求。应用自动重试可能产生新的请求。"),
    "model_concurrency_exceeded": ("模型并发超限", "应用侧排队，等待空闲后退避重试。"),
    "key_concurrency_exceeded": ("Key 并发超限", "减少该 Key 的同时请求数，等待在途请求结束。"),
    "global_concurrency_exceeded": ("网关总并发超限", "等待网关空闲后退避重试。"),
    "pool_concurrency_exceeded": ("设备组并发超限", "等待设备组空闲后重试。"),
    "rate_limit_exceeded": ("Key 请求频率超限", "降低调用频率，按 Retry-After 等待后重试。"),
    "daily_quota_exceeded": ("Key 当日调用额度用完", "等待额度重置，或联系管理员调整额度。"),
    "api_key_expired": ("API Key 已到期", "联系管理员续期。"),
    "invalid_api_key": ("API Key 无效或已停用", "检查所用 Key，或联系管理员确认 Key 状态。"),
    "model_not_allowed": ("Key 无权访问该模型", "检查模型授权和所用 API Key。"),
    "context_length_exceeded": ("上下文长度超限", "缩短历史消息或降低最大输出 Token 数。"),
    "upstream_timeout": ("上游响应超时", "核对请求长度、模型负载和应用超时设置，避免立即密集重试。"),
    "upstream_unavailable": ("上游连接不可用", "检查模型服务健康状态及网关到模型的连接。"),
    "upstream_error": ("上游返回错误", "根据 Request ID 核对模型服务日志和请求参数。"),
    "npu_quarantined": ("NPU 运行时已隔离", "先处理设备或运行时故障，再恢复模型服务。"),
    "tool_choice_not_enabled": ("自动工具调用未启用", "检查模型引擎的自动工具调用及解析器配置。"),
    "client_cancelled": ("调用连接已取消", "核对应用取消、超时及网络日志；不能仅据此认定用户点击了停止。"),
    "stream_interrupted": ("流未完整结束", "核对应用、网关和模型日志，确认断开位置。"),
    "invalid_upstream_response": ("上游响应格式无效", "检查模型响应协议及解析器。"),
    "safety_violation": ("安全审核拦截", "检查审核结果和请求内容。"),
    "guard_unavailable": ("安全审核服务不可用", "检查安全审核服务健康状态和连接。"),
    "invalid_guard_response": ("安全审核响应无效", "根据 Request ID 检查安全审核服务日志。"),
    "stream_unavailable": ("流式处理失败", "根据 Request ID 检查网关与模型日志。"),
    "internal_error": ("网关内部错误", "根据 Request ID 检查网关日志。"),
    "invalid_messages": ("消息格式错误", "messages 必须是非空消息数组。"),
    "invalid_json": ("请求 JSON 无效", "检查请求体是否为有效 JSON。"),
    "invalid_request": ("请求格式无效", "检查请求字段、类型和接口文档。"),
    "request_too_large": ("请求体过大", "缩小请求体或上传文件。"),
    "unsupported_streaming": ("接口不支持流式调用", "关闭 stream，或改用支持流式输出的接口。"),
    "historical_unknown": ("历史原因未记录", "只能核对尚存的原始日志，不能由状态推断具体原因。"),
}


REQUEST_CONTENT_MAX_CHARS = 65_536
REQUEST_STRING_MAX_CHARS = 32_768
_SENSITIVE_KEYS = {
    "authorization", "api_key", "apikey", "access_token", "refresh_token",
    "password", "passwd", "secret", "client_secret", "credential", "credentials",
}
_SENSITIVE_QUERY_KEYS = _SENSITIVE_KEYS | {"key", "signature", "sig"}
_BASE64_DATA = re.compile(r"^data:([^;,]+)?(?:;[^,]*)?;base64,", re.IGNORECASE)
_LIKELY_BASE64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


def _safe_url(value: str, state: dict) -> str:
    """Keep useful URLs while removing credentials and signed query values."""
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"}:
            return value
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        query = []
        for key, item in parse_qsl(parts.query, keep_blank_values=True):
            if key.lower() in _SENSITIVE_QUERY_KEYS:
                item = "[已隐藏]"
                state["redacted_fields"] += 1
            query.append((key, item))
        if parts.username or parts.password:
            state["redacted_fields"] += 1
        return urlunsplit((parts.scheme, host, parts.path, urlencode(query), parts.fragment))
    except (TypeError, ValueError):
        return value


def _sanitize_content(value, state: dict, *, key: str = "", depth: int = 0):
    if depth > 16:
        state["truncated"] = True
        return "[层级过深，已省略]"
    if key.lower() in _SENSITIVE_KEYS:
        state["redacted_fields"] += 1
        return "[已隐藏敏感字段]"
    if value is None or type(value) in (bool, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        match = _BASE64_DATA.match(value)
        if match:
            state["binary_fields"] += 1
            return f"[已省略 {match.group(1) or 'binary'} base64 数据，原始 {len(value)} 字符]"
        # Large base64 blobs also occur without a data URI in audio/image fields.
        compact = value.replace("\n", "").replace("\r", "")
        if len(compact) > 4096 and len(compact) % 4 == 0 and _LIKELY_BASE64.fullmatch(compact):
            state["binary_fields"] += 1
            return f"[已省略 base64 数据，原始 {len(value)} 字符]"
        if value.startswith(("http://", "https://")):
            value = _safe_url(value, state)
        if len(value) > REQUEST_STRING_MAX_CHARS:
            state["truncated"] = True
            return value[:REQUEST_STRING_MAX_CHARS] + f"\n…[已截断 {len(value) - REQUEST_STRING_MAX_CHARS} 字符]"
        return value
    if isinstance(value, dict):
        return {
            str(item_key)[:128]: _sanitize_content(item, state, key=str(item_key), depth=depth + 1)
            for item_key, item in list(value.items())[:256]
        }
    if isinstance(value, (list, tuple)):
        if len(value) > 256:
            state["truncated"] = True
        return [_sanitize_content(item, state, depth=depth + 1) for item in value[:256]]
    return str(value)[:1024]


def sanitize_request_content(value, *, raw_size: int | None = None) -> dict:
    """Return a bounded copy suitable for the authenticated administration UI.

    Text and ordinary request parameters remain readable. Credentials and binary
    media are replaced with explicit markers so the usage database cannot become
    a second media store or leak API secrets.
    """
    state = {"truncated": False, "redacted_fields": 0, "binary_fields": 0}
    payload = _sanitize_content(value, state)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > REQUEST_CONTENT_MAX_CHARS:
        state["truncated"] = True
        payload = {
            "preview": encoded[:REQUEST_CONTENT_MAX_CHARS],
            "notice": f"请求内容总长度超过 {REQUEST_CONTENT_MAX_CHARS} 字符，已截断",
        }
    return {
        "available": True,
        "payload": payload,
        "raw_bytes": raw_size if type(raw_size) is int and raw_size >= 0 else None,
        **state,
    }


def unavailable_request_content(reason: str = "请求正文未进入网关解析阶段") -> dict:
    return {
        "available": False,
        "reason": reason[:256],
        "payload": None,
        "raw_bytes": None,
        "truncated": False,
        "redacted_fields": 0,
        "binary_fields": 0,
    }


def clean_request_content(value) -> dict:
    if not isinstance(value, dict) or type(value.get("available")) is not bool:
        return unavailable_request_content("历史记录未保存请求内容")
    # Re-sanitize at the storage boundary so callers cannot bypass redaction.
    if value["available"]:
        result = sanitize_request_content(value.get("payload"), raw_size=value.get("raw_bytes"))
        result["truncated"] = bool(result["truncated"] or value.get("truncated"))
        result["redacted_fields"] = max(result["redacted_fields"], int(value.get("redacted_fields") or 0))
        result["binary_fields"] = max(result["binary_fields"], int(value.get("binary_fields") or 0))
        return result
    return unavailable_request_content(str(value.get("reason") or "请求正文未保存"))


def new_diagnostics() -> dict:
    return {
        "source": "gateway",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "_started": time.perf_counter(),
        "upstream_dispatched": False,
        "stage": "gateway",
    }


def finish_diagnostics(value: dict | None) -> dict:
    value = dict(value or {})
    if "_started" in value:
        value["elapsed_ms"] = round((time.perf_counter() - value.pop("_started")) * 1000, 3)
        value["finished_at"] = datetime.now(timezone.utc).isoformat()
    return clean_diagnostics(value)


def clean_diagnostics(value: dict | None) -> dict:
    """Keep only bounded, non-sensitive diagnostic fields."""
    result: dict = {}
    if not isinstance(value, dict):
        return result
    for key in (
        "http_status",
        "upstream_http_status",
        "elapsed_ms",
        "first_output_ms",
        "last_output_ms",
        "output_chunks",
        "limit",
        "in_flight",
        "retry_after",
    ):
        item = value.get(key)
        if type(item) in (int, float) and math.isfinite(item) and item >= 0:
            result[key] = item
    for key, choices in {
        "source": {"gateway", "historical_access_rejection"},
        "stage": {"gateway", "admission", "upstream", "stream", "guard"},
        "scope": {"global", "key", "model", "pool"},
        "finish_reason": {"stop", "length", "tool_calls", "content_filter"},
    }.items():
        if value.get(key) in choices:
            result[key] = value[key]
    if type(value.get("upstream_dispatched")) is bool:
        result["upstream_dispatched"] = value["upstream_dispatched"]
    for key in ("started_at", "finished_at"):
        if isinstance(value.get(key), str):
            try:
                result[key] = datetime.fromisoformat(value[key].replace("Z", "+00:00")).isoformat()
            except ValueError:
                pass
    code = value.get("code")
    if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
        result["code"] = code
    return result


def upstream_code(body: object, default: str = "upstream_error") -> str:
    text = str(body or "").lower()
    if any(marker in text for marker in ("timed out", "timeout", "read operation timed")):
        return "upstream_timeout"
    if any(
        marker in text
        for marker in (
            "maximum context length",
            "context_length_exceeded",
            "max_tokens is too large",
            "max_completion_tokens is too large",
        )
    ):
        return "context_length_exceeded"
    if "tool choice requires" in text or (
        "enable-auto-tool-choice" in text and "tool-call-parser" in text
    ):
        return "tool_choice_not_enabled"
    return default


def describe_diagnostics(value: dict | None, status: str) -> dict:
    result = clean_diagnostics(value)
    code = result.get("code")
    if not code and status != "succeeded":
        code = {
            "cancelled": "client_cancelled",
            "interrupted": "stream_interrupted",
        }.get(status, "historical_unknown")
    reason, suggestion = REASONS.get(
        code, ("请求处理失败", "根据错误码和 Request ID 核对日志。")
    )
    result.update(
        code=code or "",
        reason=reason if code else "请求成功",
        suggestion=suggestion if code else "",
    )
    result["not_inferred"] = (
        result.get("upstream_dispatched") is False and status != "succeeded"
    )
    return result


def initialize_diagnostics(db: object) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(usage_events)")}
    if "diagnostics" not in columns:
        db.execute("ALTER TABLE usage_events ADD COLUMN diagnostics TEXT NOT NULL DEFAULT '{}'")
    if "error_code" not in columns:
        db.execute("ALTER TABLE usage_events ADD COLUMN error_code TEXT NOT NULL DEFAULT ''")
    db.execute("CREATE INDEX IF NOT EXISTS idx_usage_error_code ON usage_events(error_code)")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_rejection_match "
        "ON access_rejections(api_key_id, request_id, model)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_request_match "
        "ON usage_events(api_key_id, request_id, model)"
    )

    # Caller-supplied Request IDs are not unique. Backfill only unique pairs in
    # both directions, using the same Key, model, and a five-second window.
    rows = db.execute(
        """SELECT u.id, r.code, r.sequence FROM usage_events u JOIN access_rejections r
        ON u.api_key_id=r.api_key_id AND u.request_id=r.request_id AND u.model=r.model
        AND abs(julianday(u.created_at)-julianday(r.created_at))*86400 <= 5
        WHERE u.status='rejected' AND u.error_code='' AND u.diagnostics='{}'
        AND (SELECT count(*) FROM access_rejections x WHERE x.api_key_id=u.api_key_id
             AND x.request_id=u.request_id AND x.model=u.model
             AND abs(julianday(u.created_at)-julianday(x.created_at))*86400 <= 5)=1
        AND (SELECT count(*) FROM usage_events x WHERE x.api_key_id=r.api_key_id
             AND x.request_id=r.request_id AND x.model=r.model AND x.status='rejected'
             AND abs(julianday(x.created_at)-julianday(r.created_at))*86400 <= 5)=1"""
    ).fetchall()
    eligible = {
        "rate_limit_exceeded",
        "daily_quota_exceeded",
        "api_key_expired",
        "model_not_allowed",
    }
    for row in rows:
        code = row["code"]
        if code not in REASONS or not (
            code.endswith("_concurrency_exceeded") or code in eligible
        ):
            continue
        value = {
            "code": code,
            "stage": "admission",
            "upstream_dispatched": False,
            "source": "historical_access_rejection",
            "http_status": 401
            if code == "api_key_expired"
            else 403
            if code == "model_not_allowed"
            else 429,
        }
        db.execute(
            "UPDATE usage_events SET error_code=?, diagnostics=? WHERE id=?",
            (code, json.dumps(value), row["id"]),
        )
