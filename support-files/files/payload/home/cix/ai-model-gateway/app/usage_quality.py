"""Loss-aware usage normalization: an absent counter is not a measured zero."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

FIELDS = ("input_tokens", "output_tokens", "total_tokens")
ALIASES = {"input_tokens": ("input_tokens", "prompt_tokens"),
           "output_tokens": ("output_tokens", "completion_tokens"), "total_tokens": ("total_tokens",)}
MAX_COUNTER = 2**53 - 1  # Exact integer range shared by SQLite and the JS console.


def counter(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 16:
        value = int(value)
    return value if isinstance(value, int) and 0 <= value <= MAX_COUNTER else None


def usage_details(value: Any, endpoint: str = "") -> dict[str, Any]:
    body = value if isinstance(value, dict) else {}
    tokens: dict[str, int | None] = dict.fromkeys(FIELDS)
    raw, sources, issues = {}, {}, []
    supplied_valid = False
    for field, names in ALIASES.items():
        present = [name for name in names if name in body]
        for name in present:
            raw[name] = body[name] if counter(body[name]) is not None else "invalid_counter"
        if present:
            name = present[0]  # Explicit zero has precedence, unlike `a or b`.
            tokens[field] = counter(body[name])
            sources[field] = name
            if tokens[field] is None:
                issues.append("invalid_" + field)
            else:
                supplied_valid = True
            if len(present) == 2 and counter(body[present[1]]) != tokens[field]:
                issues.append("conflicting_" + field + "_aliases")
    derived = False
    if endpoint in {"/v1/embeddings", "/v1/rerank"} and not any(n in body for n in ALIASES["output_tokens"]):
        # These endpoints do not generate text tokens. Do not infer missing
        # input/total, and do not use this rule for chat or OCR.
        tokens["output_tokens"] = 0
        sources["output_tokens"] = "non_generation_endpoint"
        derived = True
    if "total_tokens" not in body and tokens["input_tokens"] is not None and tokens["output_tokens"] is not None:
        total = tokens["input_tokens"] + tokens["output_tokens"]
        if total <= MAX_COUNTER:
            tokens["total_tokens"] = total
            sources["total_tokens"] = "derived_sum"
            derived = True
        else:
            issues.append("total_overflow")
    if all(v is not None for v in tokens.values()) and tokens["total_tokens"] != tokens["input_tokens"] + tokens["output_tokens"]:
        issues.append("total_mismatch")
    if any(i == "total_mismatch" or i.startswith("conflicting_") for i in issues):
        quality = "inconsistent"
    elif not supplied_valid:
        quality = "invalid" if issues else "missing"
    elif issues or any(v is None for v in tokens.values()):
        quality = "partial"
    else:
        quality = "derived" if derived else "reported"
    return {"tokens": tokens, "reported": supplied_valid, "quality": quality,
            "source": "mixed" if supplied_valid and derived else "upstream" if supplied_valid else "none",
            "raw": raw, "sources": sources, "issues": issues}


def utc_boundary(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) > 64:
        raise ValueError("日期参数过长")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("日期必须是含时区的 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValueError("日期必须包含时区，例如 +08:00 或 Z")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
