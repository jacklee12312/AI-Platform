from __future__ import annotations

import argparse
import json
import os
import time

import requests


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only ModelDeck production smoke test")
    parser.add_argument("--base-url", default="http://192.168.133.1:17780")
    args = parser.parse_args()
    password = os.environ.get("MODELDECK_PASSWORD", "")
    if not password:
        raise SystemExit("MODELDECK_PASSWORD is required")

    client = requests.Session()
    base = args.base_url.rstrip("/")
    live = client.get(f"{base}/health/live", timeout=10)
    live.raise_for_status()
    login = client.post(f"{base}/api/login", json={"password": password}, timeout=15)
    login.raise_for_status()
    csrf = login.json()["csrf"]

    overview = client.get(f"{base}/api/overview", timeout=60)
    overview.raise_for_status()
    overview_data = overview.json()
    models = client.get(f"{base}/api/models", timeout=30)
    models.raise_for_status()
    config = client.get(f"{base}/api/config", timeout=30)
    config.raise_for_status()
    config_data = config.json()
    validation = client.post(
        f"{base}/api/config/validate",
        json={
            "settings": config_data["settings"],
            "models": [
                {key: item[key] for key in ("id", "enabled", "guard", "base_url")}
                for item in config_data["models"]
            ]
        },
        timeout=30,
    )
    validation.raise_for_status()
    logs = client.get(f"{base}/api/logs", params={"service": "gateway", "tail": 20}, timeout=30)
    logs.raise_for_status()

    started = time.perf_counter()
    streamed = client.post(
        f"{base}/api/test/chat",
        json={
            "model": "copaw-flash-9b",
            "prompt": "请从1数到10，用空格分隔，不要解释。",
            "stream": True,
            "max_tokens": 48,
            "temperature": 0.1,
        },
        stream=True,
        timeout=90,
    )
    streamed.raise_for_status()
    first = None
    chunks = 0
    done = False
    for line in streamed.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done = True
            break
        event = json.loads(data)
        content = event.get("choices", [{}])[0].get("delta", {}).get("content")
        if content:
            first = first or time.perf_counter()
            chunks += 1
    ended = time.perf_counter()

    logout = client.post(f"{base}/api/logout", headers={"X-CSRF-Token": csrf}, timeout=15)
    logout.raise_for_status()
    print(
        json.dumps(
            {
                "live": live.json().get("ok"),
                "gateway": overview_data["gateway"]["status"],
                "mode": overview_data["gateway"]["active_mode"],
                "agent_healthy": overview_data["host"]["modelctl"].get("healthy"),
                "cpu_percent": overview_data["host"]["system"].get("cpu_percent"),
                "models": len(models.json().get("data", [])),
                "config_changes": len(validation.json().get("changes", [])),
                "log_lines": len(logs.json().get("lines", [])),
                "stream_status": streamed.status_code,
                "stream_chunks": chunks,
                "stream_ttft_s": round(first - started, 3) if first else None,
                "stream_total_s": round(ended - started, 3),
                "stream_done": done,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
