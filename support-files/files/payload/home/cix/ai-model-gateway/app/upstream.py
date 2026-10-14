from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class UpstreamError(Exception):
    message: str
    status_code: int | None = None
    body: str | None = None


@dataclass(frozen=True)
class RawUpstreamResponse:
    body: bytes
    status_code: int
    content_type: str
    headers: dict[str, str]


async def request_json_async(
    url: str,
    *,
    method: str = "GET",
    payload: Any = None,
    raw_body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict:
    """Use cancellable async I/O for requests whose upstream supports disconnect abort."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0), trust_env=False
        ) as client:
            response = await client.request(
                method, url, json=payload, content=raw_body, headers=headers
            )
            if response.status_code >= 400:
                raise UpstreamError(
                    f"upstream returned HTTP {response.status_code}",
                    response.status_code,
                    response.text[:500],
                )
            try:
                value = response.json()
                if not isinstance(value, dict):
                    raise ValueError("not an object")
                return value
            except ValueError as exc:
                raise UpstreamError(
                    "upstream returned invalid JSON", body=response.text[:500]
                ) from exc
    except UpstreamError:
        raise
    except httpx.HTTPError as exc:
        raise UpstreamError(
            "upstream connection failed: " + type(exc).__name__
        ) from exc


def request_bytes(
    url: str,
    *,
    method: str = "GET",
    raw_body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> RawUpstreamResponse:
    request_headers = {"User-Agent": "ai-model-gateway/0.5.0"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=raw_body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return RawUpstreamResponse(
                body=response.read(),
                status_code=response.status,
                content_type=response.headers.get_content_type(),
                headers={
                    name: value
                    for name in (
                        "X-Audio-Duration-S",
                        "X-Audio-Duration-Ms",
                        "X-Inference-Ms",
                        "X-TTS-Synthesis-Ms",
                        "X-TTS-First-Callback-Ms",
                        "X-Real-Time-Factor",
                        "X-TTS-Model",
                        "X-TTS-Chunks",
                        "X-TTS-Callback-Chunks",
                    )
                    if (value := response.headers.get(name)) is not None
                },
            )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise UpstreamError(f"upstream returned HTTP {exc.code}", exc.code, body) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
        raise UpstreamError(f"upstream connection failed: {exc}") from exc


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: Any = None,
    raw_body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict:
    request_headers = {"Accept": "application/json", "User-Agent": "ai-model-gateway/0.5.0"}
    request_headers.update(headers or {})
    data = raw_body
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise UpstreamError(f"upstream returned HTTP {exc.code}", exc.code, body) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
        raise UpstreamError(f"upstream connection failed: {exc}") from exc
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpstreamError("upstream returned invalid JSON", body=body[:500].decode("utf-8", errors="replace")) from exc
