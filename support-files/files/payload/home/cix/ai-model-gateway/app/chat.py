from __future__ import annotations

from typing import Any


def normalize_tool_history(messages: list[Any]) -> list[Any]:
    """Return an upstream-safe copy of OpenAI chat history.

    HLIELlama 2.1.0's peg-native chat parser rejects replayed assistant
    messages that contain both non-empty content and tool_calls.  OpenAI
    clients commonly replay the model response verbatim, so normalize only
    the upstream copy while keeping the client payload unchanged.
    """
    normalized: list[Any] = []
    for message in messages:
        if (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and isinstance(message.get("tool_calls"), list)
            and message["tool_calls"]
        ):
            item = dict(message)
            item["content"] = None
            normalized.append(item)
        else:
            normalized.append(message)
    return normalized
