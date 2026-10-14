"""Register the ballot-reader route on the existing gateway application.

Keeping the adapter separate lets the box reuse the currently validated
ai-model-gateway image while adding one narrowly scoped proxy endpoint.
"""

from __future__ import annotations

import uuid

from fastapi import Query, Request

from app import main as gateway
from app.errors import GatewayError


gateway.METERED_ENDPOINTS.add("/v1/documents/analyze")


def _multipart_document(body: bytes, content_type: str) -> tuple[bytes, str]:
    if content_type.lower().startswith("multipart/form-data"):
        return body, content_type
    boundary = f"ballot-reader-{uuid.uuid4().hex}"
    media_type = content_type.split(";", 1)[0] or "application/octet-stream"
    filename = "document.pdf" if media_type == "application/pdf" else "document.bin"
    prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: {media_type}\r\n\r\n"
    ).encode("ascii")
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    return prefix + body + suffix, f"multipart/form-data; boundary={boundary}"


@gateway.app.post("/v1/documents/analyze")
async def document_analyze(request: Request, model: str = Query(..., min_length=1, max_length=128)):
    entry = gateway.REGISTRY.get(model, "document_analysis")
    body = await request.body()
    if not body:
        raise GatewayError(400, "Document image is required", "empty_document", "invalid_request_error")
    if len(body) > gateway.MAX_BODY_BYTES:
        raise GatewayError(413, "Document image is too large", "request_too_large", "invalid_request_error")
    gateway._capture_request_content(
        request,
        {"model": model, "document": f"[已省略文档，原始 {len(body)} 字节]"},
        len(body),
    )
    await gateway._admit(request, entry)
    body, content_type = _multipart_document(
        body, request.headers.get("Content-Type", "application/octet-stream")
    )
    response = await gateway._call_json(
        f"{entry.base_url}/predict",
        method="POST",
        raw_body=body,
        headers={"Content-Type": content_type},
        timeout=gateway.CV_TIMEOUT,
        diagnostics=request.state.diagnostics,
    )
    await gateway._record_usage(
        request, endpoint=request.url.path, model=entry.id, usage=response.get("usage")
    )
    return {**response, "model": entry.id}


app = gateway.app

