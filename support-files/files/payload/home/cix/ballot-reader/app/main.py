from __future__ import annotations

import base64
import json
import os

from aiohttp import web

from .reader import MODEL_ID, BallotError, analyze_bytes


MAX_FILE_BYTES = int(os.getenv("BALLOT_MAX_FILE_BYTES", "26214400"))


def _json(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda value: json.dumps(value, ensure_ascii=False))


async def health(_: web.Request) -> web.Response:
    return _json({"ok": True, "service": "ballot-reader", "version": "0.1.0", "model": MODEL_ID, "device": "CPU"})


async def predict(request: web.Request) -> web.Response:
    if not request.content_type.startswith("multipart/"):
        return _json({"detail": {"code": "multipart_required", "message": "需要 multipart/form-data 图片或 PDF"}}, 400)
    try:
        multipart = await request.multipart()
        upload = None
        while True:
            field = await multipart.next()
            if field is None:
                break
            if field.name in {"image", "file"}:
                upload = field
                break
        if upload is None:
            return _json({"detail": {"code": "file_missing", "message": "缺少 image 文件字段"}}, 400)
        data = await upload.read(decode=False)
        if len(data) > MAX_FILE_BYTES:
            return _json({"detail": {"code": "file_too_large", "max_bytes": MAX_FILE_BYTES}}, 413)
        result, overlays = analyze_bytes(data, upload.filename or "document.bin")
    except BallotError as exc:
        return _json(
            {"detail": {"code": exc.code, "message": str(exc), "diagnostics": exc.diagnostics}},
            422,
        )
    except (ValueError, OSError) as exc:
        return _json({"detail": {"code": "invalid_request", "message": str(exc)}}, 400)
    if request.query.get("include_overlay", "false").lower() in {"1", "true", "yes"}:
        result["overlays"] = [base64.b64encode(item).decode("ascii") for item in overlays]
    return _json(result)


app = web.Application(client_max_size=MAX_FILE_BYTES + 1024 * 1024)
app.router.add_get("/health", health)
app.router.add_get("/health/live", health)
app.router.add_post("/predict", predict)


def main() -> None:
    web.run_app(app, host="0.0.0.0", port=17724, print=None, access_log=None)


if __name__ == "__main__":
    main()
