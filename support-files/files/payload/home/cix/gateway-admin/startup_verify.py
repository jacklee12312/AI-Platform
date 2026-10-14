#!/usr/bin/env python3
"""Run one fail-closed real-inference verification transaction per host boot."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


DEPLOY = pathlib.Path("/home/cix/houmo-docker-deployment")
STATE = DEPLOY / "state" / "startup-verification.json"
BOOT_ID = pathlib.Path("/proc/sys/kernel/random/boot_id")
AGENT_CONTAINER = "gateway-admin-agent"
AGENT_PORT = 17781
TERMINAL = {"succeeded", "failed"}
ACTIVE = {"waiting", "queued", "running"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def clean(value: object) -> str:
    text = str(value)
    if "Bearer " in text:
        text = text.split("Bearer ", 1)[0] + "Bearer [REDACTED]"
    return text[:1000]


def atomic_write(value: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_name(STATE.name + f".tmp.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o644)
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(STATE)


def load_state() -> dict:
    try:
        if STATE.stat().st_size > 131_072 or STATE.is_symlink():
            return {}
        value = json.loads(STATE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value.get("schema_version") == 1 else {}
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def agent_connection() -> tuple[str, str]:
    raw = subprocess.run(
        ["docker", "inspect", AGENT_CONTAINER], capture_output=True, text=True, check=True, timeout=15
    ).stdout
    item = json.loads(raw)[0]
    settings = dict(value.split("=", 1) for value in item["Config"]["Env"] if "=" in value)
    token = settings.get("ADMIN_AGENT_TOKEN", "")
    addresses = [row.get("IPAddress", "") for row in item["NetworkSettings"]["Networks"].values()]
    address = next((value for value in addresses if value), "")
    if not token or not address:
        raise RuntimeError("ModelDeck Agent 连接信息不完整")
    return f"http://{address}:{AGENT_PORT}", token


def request(base: str, token: str, method: str, path: str, body=None, timeout: int = 30):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method=method,
        headers={"X-Agent-Token": token, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
        except json.JSONDecodeError:
            payload = {"detail": f"HTTP {exc.code}"}
        return exc.code, payload


def write(base_state: dict, **changes) -> dict:
    value = {**base_state, **changes, "updated_at": now()}
    atomic_write(value)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="rerun on this boot after an explicit install or upgrade")
    parser.add_argument("--source", choices=("boot", "install", "upgrade", "manual"), default="boot")
    args = parser.parse_args(argv)
    boot_id = BOOT_ID.read_text(encoding="utf-8").strip()
    mode = (DEPLOY / "state" / "active-mode").read_text(encoding="utf-8").strip()
    if mode not in {"copaw", "spark-x1", "qwen38", "qwen36", "qwen-vl", "glm-ocr", "bge-m3", "reranker", "cosyvoice"}:
        raise RuntimeError(f"不支持的当前模式：{mode}")

    base, token = agent_connection()
    status, host = request(base, token, "GET", "/v1/status", timeout=45)
    if status >= 300:
        raise RuntimeError(str(host.get("detail") or "无法读取 ModelDeck Agent 状态"))
    previous = load_state()
    runtime = host.get("runtime_safety") or {}
    running_rows = [row for row in (runtime.get("services") or {}).values() if row.get("running")]
    running_names = {name for name, row in (runtime.get("services") or {}).items() if row.get("running")}
    previously_verified = set(previous.get("verified_models") or [])
    all_verified = bool(running_rows) and running_names == previously_verified and all(
        row.get("verified") is True and row.get("health") == "healthy" for row in running_rows
    ) and not any(runtime.get(key) for key in ("incident", "maintenance", "monitor_error"))
    if not args.force and previous.get("boot_id") == boot_id:
        if previous.get("status") == "succeeded" and all_verified:
            print("Startup verification already passed for this boot; no inference was repeated.", flush=True)
            return 0
        if previous.get("status") in ACTIVE:
            raise RuntimeError("本次开机已有验证任务，未重复提交")
        if previous.get("status") == "failed":
            raise RuntimeError("本次开机验证已经失败；请检查记录并显式恢复，系统不会自动重试")
        if previous.get("status") == "succeeded":
            raise RuntimeError("验证后的模型实例在本次开机中发生变化，保持隔离并等待显式恢复")

    state = write({"schema_version": 1, "boot_id": boot_id, "source": args.source,
                   "mode": mode, "status": "waiting", "started_at": now()},
                  stage="等待 NPU 安全监控与模型状态")
    monitor_deadline = time.monotonic() + 90
    while runtime.get("monitor_error") and not runtime.get("incident") and time.monotonic() < monitor_deadline:
        state = write(state, stage="等待 NPU 日志监控完成本次开机检查")
        time.sleep(2)
        status, host = request(base, token, "GET", "/v1/status", timeout=45)
        if status >= 300:
            raise RuntimeError(str(host.get("detail") or "无法读取 ModelDeck Agent 状态"))
        runtime = host.get("runtime_safety") or {}
    if runtime.get("incident"):
        raise RuntimeError("存在尚未恢复的 NPU 故障，开机任务不会自动解除隔离")
    if runtime.get("maintenance"):
        raise RuntimeError("已有设备维护任务，开机任务未重复提交")
    if runtime.get("monitor_error"):
        raise RuntimeError("NPU 日志监控未就绪，开机任务保持隔离")

    status, job = request(base, token, "POST", "/v1/startup-verification", {"mode": mode})
    if status >= 300 or not job.get("id"):
        raise RuntimeError(str(job.get("detail") or "开机真实推理验证未被接受"))
    state = write(state, status=job.get("status", "queued"), job_id=job["id"], stage="已提交开机真实推理验证")
    print(f"Submitted startup verification: job={job['id']} mode={mode}", flush=True)
    deadline = time.monotonic() + 1200
    last_stage = None
    while time.monotonic() < deadline:
        status, current = request(base, token, "GET", "/v1/jobs/" + job["id"])
        if status >= 300:
            raise RuntimeError(str(current.get("detail") or "无法读取开机验证任务"))
        progress = current.get("progress") or {}
        stage = progress.get("stage") or state.get("stage")
        changes = {"status": current.get("status", "unknown"), "stage": stage}
        for key in ("current_model", "completed", "total", "elapsed_seconds"):
            if key in progress:
                changes[key] = progress[key]
        state = write(state, **changes)
        if stage and stage != last_stage:
            print(f"Startup verification progress: {stage}", flush=True)
            last_stage = stage
        if current.get("status") in TERMINAL:
            result = current.get("result") or {}
            verification = result.get("verification") or {}
            expected = result.get("expected_models") or []
            verified = verification.get("verified_instances") or {}
            if current["status"] != "succeeded" or verification.get("status") != "succeeded" or set(verified) != set(expected):
                raise RuntimeError(str(current.get("error") or "开机验证没有覆盖全部目标模型"))
            state = write(state, status="succeeded", stage="开机真实推理验证通过，网关已开放调用",
                          completed=len(expected), total=len(expected), finished_at=now(),
                          evidence_id=verification.get("evidence_id"), verified_models=expected,
                          already_verified=bool(result.get("already_verified")), error=None)
            print(f"Startup verification passed: evidence={state.get('evidence_id')} models={len(expected)}", flush=True)
            return 0
        time.sleep(2)
    raise RuntimeError("开机真实推理验证超过 1200 秒")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, RuntimeError, subprocess.SubprocessError, urllib.error.URLError) as exc:
        try:
            previous = load_state()
            boot_id = BOOT_ID.read_text(encoding="utf-8").strip()
            base_state = previous if previous.get("boot_id") == boot_id else {
                "schema_version": 1, "boot_id": boot_id, "source": "boot", "started_at": now()
            }
            write(base_state, status="failed", stage="开机真实推理验证失败，网关保持隔离",
                  error=clean(exc), finished_at=now())
        except Exception:
            pass
        print(f"ERROR: {clean(exc)}", file=sys.stderr)
        raise SystemExit(1)
