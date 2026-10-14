from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx


BASE_URL = os.getenv("MODELDECK_URL", "http://192.168.133.1:17780").rstrip("/")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://192.168.133.1:17700").rstrip("/")


def wait_job(client: httpx.Client, job_id: str, timeout: float = 600) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"{BASE_URL}/api/jobs/{job_id}")
        response.raise_for_status()
        job = response.json()
        if job["status"] == "succeeded":
            return job
        if job["status"] == "failed":
            raise RuntimeError(job.get("error") or "configuration job failed")
        time.sleep(2)
    raise TimeoutError(f"configuration job did not finish in {timeout:.0f}s")


def config_payload(settings: dict[str, Any], models: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "settings": settings,
        "models": [
            {key: item[key] for key in ("id", "enabled", "guard", "base_url")}
            for item in models
        ],
    }


def apply_settings(
    client: httpx.Client,
    csrf: str,
    settings: dict[str, Any],
    models: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = config_payload(settings, models)
    validation_response = client.post(f"{BASE_URL}/api/config/validate", json=payload)
    validation_response.raise_for_status()
    validation = validation_response.json()
    apply_response = client.post(
        f"{BASE_URL}/api/config/apply",
        json=payload,
        headers={"X-CSRF-Token": csrf},
    )
    apply_response.raise_for_status()
    return validation, wait_job(client, apply_response.json()["id"])


def verify_generation_defaults(api_key: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(timeout=120) as client:
        models_response = client.get(f"{GATEWAY_URL}/v1/models", headers=headers)
        models_response.raise_for_status()
        online_chat = next(
            item
            for item in models_response.json()["data"]
            if item.get("capability") == "chat" and item.get("status") == "online"
        )
        response = client.post(
            f"{GATEWAY_URL}/v1/chat/completions",
            headers=headers,
            json={
                "model": online_chat["id"],
                "messages": [{"role": "user", "content": "只回复 OK"}],
                "stream": False,
            },
        )
        response.raise_for_status()
        applied = response.headers.get("X-Generation-Defaults", "")
        expected = {"max_tokens", "temperature", "top_p"}
        if set(filter(None, applied.split(","))) != expected:
            raise AssertionError(f"unexpected generation defaults header: {applied}")
        return {
            "model": online_chat["id"],
            "status": response.status_code,
            "defaults_header": applied,
            "completion_tokens": response.json().get("usage", {}).get("completion_tokens"),
        }


def main() -> None:
    password = os.getenv("MODELDECK_PASSWORD", "")
    api_key = os.getenv("GATEWAY_API_KEY") or os.getenv("HOUMO_API_KEY", "")
    if not password:
        raise SystemExit("MODELDECK_PASSWORD is required")
    if not api_key:
        raise SystemExit("GATEWAY_API_KEY or HOUMO_API_KEY is required")

    report: dict[str, Any] = {}
    with httpx.Client(timeout=30) as client:
        login = client.post(f"{BASE_URL}/api/login", json={"password": password})
        login.raise_for_status()
        csrf = login.json()["csrf"]
        current_response = client.get(f"{BASE_URL}/api/config")
        current_response.raise_for_status()
        current = current_response.json()
        original = dict(current["settings"])
        temporary = dict(original)
        temporary.update(
            {
                "generation_defaults_enabled": True,
                "default_max_tokens": 7,
                "chat_timeout_seconds": 301 if original["chat_timeout_seconds"] != 301 else 300,
                "guard_cpu_threads": 6,
                "guard_max_streams": 3,
                "asr_num_threads": 6,
                "asr_max_concurrency": 3,
            }
        )
        applied = False
        try:
            started = time.monotonic()
            validation, job = apply_settings(client, csrf, temporary, current["models"])
            applied = True
            report["temporary_apply_seconds"] = round(time.monotonic() - started, 2)
            report["affected_services"] = validation["affected_services"]
            report["backup_id"] = job.get("result", {}).get("backup_id")
            after = client.get(f"{BASE_URL}/api/config")
            after.raise_for_status()
            effective = after.json()["settings"]
            for key, value in temporary.items():
                if effective[key] != value:
                    raise AssertionError(f"setting did not persist: {key}={effective[key]!r}, expected {value!r}")
            report["generation_request"] = verify_generation_defaults(api_key)
        finally:
            if applied:
                restored_validation, restored_job = apply_settings(client, csrf, original, current["models"])
                restored = client.get(f"{BASE_URL}/api/config")
                restored.raise_for_status()
                if restored.json()["settings"] != original:
                    raise AssertionError("original runtime settings were not fully restored")
                report["restored_services"] = restored_validation["affected_services"]
                report["restore_backup_id"] = restored_job.get("result", {}).get("backup_id")
        logout = client.post(f"{BASE_URL}/api/logout", headers={"X-CSRF-Token": csrf})
        logout.raise_for_status()

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
