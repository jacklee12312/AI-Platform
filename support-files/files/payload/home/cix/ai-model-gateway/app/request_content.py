"""Bounded request content and metadata for the authenticated admin UI."""
from __future__ import annotations

import json
import math
import re

from .request_diagnostics import (
    clean_request_content,
    sanitize_request_content,
    unavailable_request_content,
)


def request_summary(content: dict | None) -> str:
    """Return a short, sanitized description suitable for list responses."""
    content = content or {}
    if not content.get("available"):
        return str(content.get("reason") or "历史记录未保存请求内容")[:180]
    payload = content.get("payload")
    value = payload
    if isinstance(payload, dict):
        messages = payload.get("messages")
        if isinstance(messages, list):
            user = next(
                (
                    message
                    for message in reversed(messages)
                    if isinstance(message, dict) and message.get("role") == "user"
                ),
                {},
            )
            value = user.get("content", payload)
        else:
            value = payload.get("input", payload.get("query", payload))
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return " ".join(text.split())[:180] or "已记录空请求体"


def clean_request_metadata(value: object) -> dict:
    """Keep bounded connection and generation metadata; never raw headers."""
    if not isinstance(value, dict):
        return {}
    result: dict = {}
    for key in (
        "client_ip",
        "user_agent",
        "method",
        "content_type",
        "application",
    ):
        item = value.get(key)
        if isinstance(item, str):
            result[key] = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [已隐藏]", item[:256])
    for key in ("client_port", "message_count", "tool_count", "raw_bytes"):
        item = value.get(key)
        if type(item) is int and item >= 0:
            result[key] = item
    if type(value.get("stream")) is bool:
        result["stream"] = value["stream"]
    parameters = value.get("effective_parameters")
    if isinstance(parameters, dict):
        safe: dict = {}
        for key in (
            "max_tokens",
            "max_completion_tokens",
            "temperature",
            "top_p",
            "top_k",
            "presence_penalty",
            "frequency_penalty",
            "repetition_penalty",
            "seed",
            "n",
        ):
            item = parameters.get(key)
            if type(item) in (int, float) and math.isfinite(item):
                safe[key] = item
        tool_choice = parameters.get("tool_choice")
        if isinstance(tool_choice, str) and tool_choice in {"auto", "none", "required"}:
            safe["tool_choice"] = tool_choice
        result["effective_parameters"] = safe
    return result


__all__ = [
    "clean_request_content",
    "sanitize_request_content",
    "unavailable_request_content",
    "request_summary",
    "clean_request_metadata",
]
