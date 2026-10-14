from __future__ import annotations

from typing import Any


def apply_generation_defaults(
    payload: dict[str, Any],
    *,
    enabled: bool,
    max_tokens: int,
    temperature: float,
    top_p: float,
) -> tuple[dict[str, Any], list[str]]:
    """Fill omitted generation fields without overriding caller choices."""
    value = dict(payload)
    applied: list[str] = []
    if not enabled:
        return value, applied
    if "max_tokens" not in value and "max_completion_tokens" not in value:
        value["max_tokens"] = max_tokens
        applied.append("max_tokens")
    if "temperature" not in value:
        value["temperature"] = temperature
        applied.append("temperature")
    if "top_p" not in value:
        value["top_p"] = top_p
        applied.append("top_p")
    return value, applied
