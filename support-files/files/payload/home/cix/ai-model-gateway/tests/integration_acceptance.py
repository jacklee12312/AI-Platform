from __future__ import annotations

import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def call(
    base_url: str,
    api_key: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    raw_body: bytes | None = None,
    content_type: str = "application/json",
    auth: bool = True,
    timeout: float = 300.0,
) -> tuple[int, dict[str, Any], float]:
    headers = {"Accept": "application/json"}
    if auth:
        headers["Authorization"] = f"Bearer {api_key}"
    data = raw_body
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if data is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(f"{base_url.rstrip('/')}{path}", data=data, headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read()
    elapsed = time.perf_counter() - started
    return status, json.loads(body.decode("utf-8")), elapsed


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://192.168.133.1:17700")
    parser.add_argument("--image", default="/home/cix/cv_deploy/test.jpg")
    parser.add_argument("--skip-cv", action="store_true")
    args = parser.parse_args()

    api_key = os.environ.get("HOUMO_API_KEY", "")
    require(bool(api_key), "HOUMO_API_KEY is required")
    results: list[dict[str, Any]] = []

    def record(name: str, status: int, elapsed: float, **details: Any) -> None:
        require(status == 200, f"{name} returned HTTP {status}: {details}")
        results.append({"name": name, "status": "PASS", "seconds": round(elapsed, 3), **details})

    status, health, elapsed = call(args.base_url, api_key, "/health", auth=False)
    require(status == 200 and health["status"] == "ok" and health["error_models"] == 0, f"unhealthy gateway: {health}")
    mode = health["active_mode"]
    record("health", status, elapsed, mode=mode, online=health["online_models"])

    status, body, elapsed = call(args.base_url, api_key, "/v1/models", auth=False)
    require(status == 401 and body["error"]["code"] == "invalid_api_key", f"authentication check failed: {body}")
    results.append({"name": "missing_auth", "status": "PASS", "seconds": round(elapsed, 3)})

    inactive_model = "copaw-flash-9b" if mode == "qwen38" else "qwen3.8-27b"
    status, body, elapsed = call(
        args.base_url,
        api_key,
        "/v1/chat/completions",
        payload={"model": inactive_model, "messages": [{"role": "user", "content": "test"}]},
    )
    require(status == 503 and body["error"]["code"] == "model_inactive", f"inactive model check failed: {body}")
    results.append({"name": "inactive_model", "status": "PASS", "seconds": round(elapsed, 3), "model": inactive_model})

    active_chat_model = "qwen3.8-27b" if mode == "qwen38" else "copaw-flash-9b"
    status, body, elapsed = call(
        args.base_url,
        api_key,
        "/v1/chat/completions",
        payload={
            "model": active_chat_model,
            "messages": [{"role": "user", "content": "只回答：验收通过"}],
            "temperature": 0,
            "max_tokens": 32,
        },
    )
    require(body.get("choices"), f"chat response missing choices: {body}")
    record("guarded_chat", status, elapsed, model=active_chat_model)

    if mode == "multi-model":
        documents = ["电子数据应及时固定并校验哈希。", "今天阳光很好。", "审讯过程应当依法记录。"]
        status, body, elapsed = call(
            args.base_url,
            api_key,
            "/v1/embeddings",
            payload={"model": "bge-m3", "input": documents},
        )
        require(len(body.get("data", [])) == 3 and len(body["data"][0]["embedding"]) == 1024, f"invalid embeddings: {body}")
        record("embeddings", status, elapsed, vectors=3, dimensions=1024)

        status, rerank_body, elapsed = call(
            args.base_url,
            api_key,
            "/v1/rerank",
            payload={
                "model": "bge-reranker-v2-m3",
                "query": "如何保全电子证据",
                "documents": documents,
                "top_n": 2,
            },
        )
        require(rerank_body.get("results") and rerank_body["results"][0]["index"] == 0, f"invalid rerank: {rerank_body}")
        record("rerank", status, elapsed, top_index=rerank_body["results"][0]["index"])

        context = documents[rerank_body["results"][0]["index"]]
        status, body, elapsed = call(
            args.base_url,
            api_key,
            "/v1/chat/completions",
            payload={
                "model": "copaw-flash-9b",
                "messages": [{"role": "user", "content": f"仅根据材料回答如何保全电子证据。材料：{context}"}],
                "temperature": 0,
                "max_tokens": 96,
            },
        )
        require(body.get("choices"), f"invalid RAG chat: {body}")
        record("rag_chain", status, elapsed)

        image_path = Path(args.image)
        if image_path.is_file():
            image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
            status, body, elapsed = call(
                args.base_url,
                api_key,
                "/v1/chat/completions",
                payload={
                    "model": "qwen3-vl-8b",
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "简要描述图片。"},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
                        ],
                    }],
                    "temperature": 0,
                    "max_tokens": 64,
                },
            )
            require(body.get("choices"), f"invalid VLM response: {body}")
            record("vlm", status, elapsed)

            if not args.skip_cv:
                raw_image = image_path.read_bytes()
                for operation, model in (
                    ("classify", "mobilenet_v2"),
                    ("detect", "yolov11_n"),
                    ("face", "retinaface"),
                    ("segment", "yolov8l_seg"),
                ):
                    status, _, elapsed = call(
                        args.base_url,
                        api_key,
                        f"/v1/cv/{operation}?model={model}",
                        raw_body=raw_image,
                        content_type="image/jpeg",
                    )
                    record(f"cv_{operation}", status, elapsed, model=model)

    print(json.dumps({"result": "PASS", "mode": mode, "checks": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
