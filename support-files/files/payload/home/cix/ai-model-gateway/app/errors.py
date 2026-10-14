from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class GatewayError(Exception):
    status_code: int
    message: str
    code: str
    error_type: str = "gateway_error"
    param: str | None = None
    details: Any = None


def error_payload(error: GatewayError, request_id: str) -> dict:
    payload = {
        "error": {
            "message": error.message,
            "type": error.error_type,
            "param": error.param,
            "code": error.code,
        },
        "request_id": request_id,
    }
    if error.details is not None:
        payload["error"]["details"] = error.details
    return payload
