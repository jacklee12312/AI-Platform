from __future__ import annotations

import concurrent.futures
import json
import os
import time
import urllib.error
import urllib.request


BASE_URL = os.environ.get("GATEWAY_URL", "http://192.168.133.1:17700").rstrip("/")
API_KEY = os.environ.get("HOUMO_API_KEY", "")


def request(path: str, body: bytes, content_type: str = "application/json", timeout: float = 300.0):
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": content_type},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            payload = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = exc.read()
    return status, json.loads(payload.decode("utf-8")), time.perf_counter() - started


def chat(model: str, content: str, max_tokens: int = 32):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }, ensure_ascii=False).encode("utf-8")
    return request("/v1/chat/completions", body)


def main() -> int:
    if not API_KEY:
        raise RuntimeError("HOUMO_API_KEY is required")
    with urllib.request.urlopen(f"{BASE_URL}/health", timeout=20) as response:
        health = json.load(response)
    mode = health["active_mode"]
    model = "qwen3.8-27b" if mode == "qwen38" else "copaw-flash-9b"
    checks = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(chat, model, f"只回答OK-{index}", 16) for index in range(4)]
        concurrent_results = [future.result() for future in futures]
    if any(status != 200 for status, _, _ in concurrent_results):
        raise AssertionError(f"concurrent chat failed: {concurrent_results}")
    checks.append({
        "name": "four_concurrent_chats",
        "status": "PASS",
        "wall_seconds_max": round(max(elapsed for _, _, elapsed in concurrent_results), 3),
        "request_seconds": [round(elapsed, 3) for _, _, elapsed in concurrent_results],
    })

    long_content = "审讯记录内容。" * 800 + "\n只回答：长文本测试通过"
    status, payload, elapsed = chat(model, long_content, 32)
    if status != 200 or not payload.get("choices"):
        raise AssertionError(f"long prompt failed: HTTP {status} {payload}")
    checks.append({"name": "long_prompt_5600_chars", "status": "PASS", "seconds": round(elapsed, 3)})

    status, payload, elapsed = request("/v1/chat/completions", b"{")
    if status != 400 or payload.get("error", {}).get("code") != "invalid_json":
        raise AssertionError(f"invalid JSON handling failed: HTTP {status} {payload}")
    checks.append({"name": "invalid_json", "status": "PASS", "seconds": round(elapsed, 3)})

    status, payload, elapsed = request(
        "/v1/cv/detect?model=yolov11_n",
        b"not-an-image",
        content_type="image/jpeg",
    )
    if status != 502 or payload.get("error", {}).get("code") != "upstream_error":
        raise AssertionError(f"damaged image handling failed: HTTP {status} {payload}")
    checks.append({"name": "damaged_image", "status": "PASS", "seconds": round(elapsed, 3)})

    print(json.dumps({"result": "PASS", "mode": mode, "model": model, "checks": checks}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
