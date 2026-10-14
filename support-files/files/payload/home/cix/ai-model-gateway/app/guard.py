from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GuardAssessment:
    severity: str
    categories: tuple[str, ...]
    refusal: str | None
    raw: str

    @property
    def flagged(self) -> bool:
        return self.severity.lower() == "unsafe"


def parse_guard_output(text: str) -> GuardAssessment:
    severity_match = re.search(
        r"(?:^|\b)Safety:\s*(Safe|Unsafe|Controversial)(?=\s|Categories:|Refusal:|$)",
        text,
        re.IGNORECASE,
    )
    if not severity_match:
        raise ValueError(f"unrecognized Qwen3Guard output: {text[:200]!r}")
    severity = severity_match.group(1).capitalize()
    categories_match = re.search(r"Categories:\s*(.*?)(?=Refusal:|$)", text, re.DOTALL | re.IGNORECASE)
    categories: tuple[str, ...] = ()
    if categories_match:
        value = categories_match.group(1).strip()
        if value and value.lower() != "none":
            categories = tuple(item.strip() for item in value.split(",") if item.strip())
    refusal_match = re.search(r"Refusal:\s*(Yes|No)(?=\s|$)", text, re.IGNORECASE)
    refusal = refusal_match.group(1).capitalize() if refusal_match else None
    return GuardAssessment(severity, categories, refusal, text)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        has_image = False
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "input_text"}:
                pieces.append(str(item.get("text", "")))
            if isinstance(item, dict) and item.get("type") in {"image_url", "input_image"}:
                has_image = True
        text = "\n".join(piece for piece in pieces if piece)
        if has_image and not text:
            return "[Image content without accompanying text]"
        return text
    return ""


def messages_for_guard(messages: list[dict], include_assistant: bool = False) -> list[dict]:
    sanitized = []
    for index, message in enumerate(messages):
        role = message.get("role")
        if role not in {"system", "user", "assistant"}:
            continue
        if role == "assistant" and not include_assistant and index == len(messages) - 1:
            continue
        text = _content_text(message.get("content"))
        if text:
            sanitized.append({"role": role, "content": text})
    return sanitized


def moderation_result(assessment: GuardAssessment) -> dict:
    normalized = {
        re.sub(r"[^a-z0-9]+", "_", category.lower()).strip("_"): True
        for category in assessment.categories
    }
    return {
        "flagged": assessment.flagged,
        "severity": assessment.severity,
        "categories": normalized,
        "category_scores": {key: 1.0 for key in normalized},
        "refusal": assessment.refusal,
    }
