from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


BASE_URL = os.getenv("GATEWAY_URL", "http://192.168.133.1:17700").rstrip("/")
MANAGEMENT_TOKEN = os.getenv("GATEWAY_MANAGEMENT_TOKEN", "")


def request(method: str, path: str, *, body=None, key: str | None = None, management: bool = False, timeout: int = 300):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if management:
        headers["X-Gateway-Admin-Token"] = MANAGEMENT_TOKEN
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    with urllib.request.urlopen(
        urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method=method),
        timeout=timeout,
    ) as response:
        return response.status, json.load(response)


def stream_chat(model: str, key: str) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "只回复：流式验收通过"}],
        "max_tokens": 24,
        "temperature": 0,
        "stream": True,
    }
    request_value = urllib.request.Request(
        f"{BASE_URL}/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    usage = None
    done = False
    with urllib.request.urlopen(request_value, timeout=300) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            value = line[5:].strip()
            if value == "[DONE]":
                done = True
                break
            event = json.loads(value)
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
    if not done or usage is None:
        raise AssertionError("stream did not include both usage and [DONE]")
    return usage


def main() -> int:
    if not MANAGEMENT_TOKEN:
        raise RuntimeError("GATEWAY_MANAGEMENT_TOKEN is required")
    _, health = request("GET", "/health")
    model = "copaw-flash-9b" if health.get("active_mode") == "multi-model" else "qwen3.8-27b"
    created: list[dict] = []
    try:
        for suffix in ("A", "B"):
            _, value = request(
                "POST", "/internal/api-keys", body={"name": f"__acceptance__{suffix}"}, management=True
            )
            created.append(value)

        _, completion = request(
            "POST",
            "/v1/chat/completions",
            key=created[0]["key"],
            body={
                "model": model,
                "messages": [{"role": "user", "content": "只回复：非流式验收通过"}],
                "max_tokens": 24,
                "temperature": 0,
            },
        )
        nonstream_usage = completion.get("usage")
        if not isinstance(nonstream_usage, dict):
            raise AssertionError("non-stream response did not report usage")
        stream_usage = stream_chat(model, created[1]["key"])

        summaries = []
        for item in created:
            _, report = request(
                "GET", f"/internal/usage?api_key_id={item['id']}", management=True
            )
            summary = report["summary"]
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                if field not in summary or summary[field] <= 0:
                    raise AssertionError(f"{item['name']} has invalid {field}: {summary.get(field)}")
            if summary["request_count"] != 1:
                raise AssertionError(f"usage isolation failed for {item['name']}: {summary}")
            summaries.append(summary)

        request(
            "PATCH",
            f"/internal/api-keys/{created[0]['id']}",
            body={"enabled": False},
            management=True,
        )
        try:
            request("GET", "/v1/models", key=created[0]["key"])
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
        else:
            raise AssertionError("disabled API Key was still accepted")

        print(json.dumps({
            "ok": True,
            "model": model,
            "nonstream_usage": nonstream_usage,
            "stream_usage": stream_usage,
            "per_key_summary": summaries,
        }, ensure_ascii=False))
        return 0
    finally:
        for item in created:
            try:
                request("DELETE", f"/internal/api-keys/{item['id']}", management=True)
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
