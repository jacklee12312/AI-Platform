from __future__ import annotations

import json
from typing import Any

from .guard import GuardAssessment


def stream_assessment(payload: dict[str, Any]) -> GuardAssessment:
    value = payload.get("assessment")
    if not isinstance(value, dict):
        raise ValueError("stream Guard response has no assessment")
    severity = str(value.get("severity", ""))
    if severity not in {"Safe", "Unsafe", "Controversial"}:
        raise ValueError("stream Guard returned an invalid severity")
    category = value.get("category")
    categories = (str(category),) if category else ()
    return GuardAssessment(severity=severity, categories=categories, refusal=None, raw=json.dumps(value))


def sse_event(value: dict[str, Any] | str) -> bytes:
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return f"data: {data}\n\n".encode("utf-8")
