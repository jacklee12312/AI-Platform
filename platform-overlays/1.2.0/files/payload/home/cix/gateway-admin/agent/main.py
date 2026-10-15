from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlparse
from urllib.request import urlopen

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .configuration import CONFIG_SCHEMA, RuntimeSettings, prepare_runtime_texts, read_runtime_settings
from .device_recovery import DEVICE, Recovery, RecoveryFailure, device_lock
from .diagnostics import bounded_command, collect as collect_diagnostics
from .runtime_safety import RuntimeSafety
from . import model_api, evaluation_api
from .model_store import Catalog, atomic_json


AGENT_TOKEN = os.getenv("ADMIN_AGENT_TOKEN", "")
DOCKER = os.getenv("DOCKER_BIN", "/usr/bin/docker")
MODELCTL = os.getenv("MODELCTL_PATH", "/host-deployment/modelctl")
GATEWAY_DIR = Path(os.getenv("GATEWAY_HOST_DIR", "/host-gateway"))
MODEL_REGISTRY = GATEWAY_DIR / "config" / "models.json"
GATEWAY_COMPOSE = GATEWAY_DIR / "compose.yaml"
HOUMO_DEPLOY_DIR = Path(os.getenv("HOUMO_DEPLOY_DIR", "/host-deployment"))
HOUMO_COMPOSE = Path(os.getenv("HOUMO_COMPOSE_FILE", str(HOUMO_DEPLOY_DIR / "compose.yaml")))
SENSEVOICE_DIR = Path(os.getenv("SENSEVOICE_HOST_DIR", "/host-sensevoice"))
SENSEVOICE_COMPOSE = SENSEVOICE_DIR / "compose.yaml"

CONTAINERS = {
    "gateway": "ai-model-gateway",
    "copaw": "houmo-copaw",
    "bge-m3": "houmo-bge-m3",
    "reranker": "houmo-reranker",
    "qwen-vl": "houmo-qwen3-vl-8b",
    "glm-ocr": "houmo-glm-ocr-0.9b",
    "qwen38": "houmo-qwen38-27b",
    "qwen36": "houmo-qwen36-35b-a3b",
    "spark-x1": "houmo-spark-x1",
    "cosyvoice": "houmo-cosyvoice3",
    "cv": "cix-cv",
    "sensevoice": "houmo-sensevoice-cpu",
    "matcha": "houmo-matcha-tts",
    "kokoro": "houmo-kokoro-tts",
}

MODE_SERVICES = {
    "copaw": {"copaw"},
    "spark-x1": {"spark-x1"},
    "bge-m3": {"bge-m3"},
    "reranker": {"reranker"},
    "qwen-vl": {"qwen-vl"},
    "glm-ocr": {"glm-ocr"},
    "qwen38": {"qwen38"},
    "qwen36": {"qwen36"},
    "cosyvoice": {"cosyvoice"},
}

VALID_MODES = frozenset(mode for modes in MODE_SERVICES.values() for mode in modes)

HEALTH_PORTS = {"gateway": 17700, "copaw": 17701, "bge-m3": 17702, "reranker": 17703,
                "qwen-vl": 17705, "glm-ocr": 17709, "qwen38": 17707, "qwen36": 17712, "spark-x1": 17711,
                "cosyvoice": 8091, "cv": 17710, "sensevoice": 17708, "matcha": 17723, "kokoro": 17722}


def _wait_restart_ready(service: str, timeout: float = 360) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = _docker("inspect", CONTAINERS[service], timeout=10)
        if value.returncode:
            raise RuntimeError("容器不可读取，无法确认重启结果")
        item = json.loads(value.stdout)[0]
        state = item["State"]
        if not state.get("Running") or state.get("Restarting"):
            raise RuntimeError("重启后容器退出或再次重启，请检查日志；未自动重试")
        health = state.get("Health", {}).get("Status")
        if health == "healthy":
            return {"ready": True, "verification": "docker_health", "started_at": state.get("StartedAt")}
        if health is None:
            addresses = [v.get("IPAddress") for v in item["NetworkSettings"]["Networks"].values() if v.get("IPAddress")]
            if addresses:
                try:
                    suffix = "/health/live" if service == "gateway" else "/health"
                    with urlopen(f"http://{addresses[0]}:{HEALTH_PORTS[service]}{suffix}", timeout=3) as response:
                        if response.status == 200:
                            return {"ready": True, "verification": "http_health", "started_at": state.get("StartedAt")}
                except (OSError, ValueError):
                    pass
        time.sleep(1)
    raise RuntimeError("容器已执行重启，但健康检查超时；不代表服务已就绪，请查看日志")

RUNTIME_SAFETY = None


@asynccontextmanager
async def lifespan(_):
    global RUNTIME_SAFETY
    if os.getenv('RUNTIME_SAFETY_ENABLED', 'false').lower() == 'true':
        RUNTIME_SAFETY = RuntimeSafety(HOUMO_DEPLOY_DIR, DOCKER)
        await asyncio.to_thread(RUNTIME_SAFETY.start)
    try:
        yield
    finally:
        await asyncio.to_thread(evaluation_api.close)
        if RUNTIME_SAFETY:
            await asyncio.to_thread(RUNTIME_SAFETY.close)
            RUNTIME_SAFETY = None


app = FastAPI(title="ModelDeck restricted agent", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
OPERATION_LOCK = threading.Lock()
DIAGNOSTICS_LOCK = asyncio.Lock()
DIAGNOSTICS_CACHE: dict[str, Any] = {}


class ModeRequest(BaseModel):
    mode: str


class RecoveryRequest(BaseModel):
    mode: str
    confirmation: Literal["恢复设备"]


class ConfigModel(BaseModel):
    id: str
    enabled: bool
    base_url: str


class ConfigRequest(BaseModel):
    settings: RuntimeSettings
    models: list[ConfigModel] = Field(min_length=1)


def require_token(x_agent_token: str = Header(default="")) -> None:
    if not AGENT_TOKEN or not secrets.compare_digest(x_agent_token, AGENT_TOKEN):
        raise HTTPException(status_code=401, detail="invalid agent token")


def _run(command: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return _run([DOCKER, *args], timeout=timeout)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _parse_percent(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return float(value.strip().rstrip("%"))
    except ValueError:
        return 0.0


def _container_rows() -> list[dict[str, Any]]:
    names = list(CONTAINERS.values())
    inspected = _docker("ps", "-a", "--format", "{{json .}}", timeout=20)
    rows: dict[str, dict[str, Any]] = {}
    if inspected.returncode == 0:
        for line in inspected.stdout.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = item.get("Names", "")
            if name in names:
                rows[name] = {
                    "name": name,
                    "state": item.get("State", "unknown").lower(),
                    "status": item.get("Status", "unknown"),
                    "image": item.get("Image", ""),
                }

    running = [name for name, row in rows.items() if row["state"] == "running"]
    if running:
        stats = _docker("stats", "--no-stream", "--format", "{{json .}}", *running, timeout=25)
        if stats.returncode == 0:
            for line in stats.stdout.splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name = item.get("Name", "")
                if name in rows:
                    rows[name].update(
                        cpu_percent=_parse_percent(item.get("CPUPerc")),
                        memory=item.get("MemUsage", ""),
                        memory_percent=_parse_percent(item.get("MemPerc")),
                        network=item.get("NetIO", ""),
                    )

    return [
        {
            "service": service,
            **rows.get(name, {"name": name, "state": "absent", "status": "absent", "image": ""}),
        }
        for service, name in CONTAINERS.items()
    ]


def _read_cpu() -> tuple[int, int]:
    fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
    values = [int(value) for value in fields]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def _system_metrics() -> dict[str, Any]:
    total1, idle1 = _read_cpu()
    time.sleep(0.12)
    total2, idle2 = _read_cpu()
    total_delta = max(total2 - total1, 1)
    cpu_percent = round(100 * (1 - ((idle2 - idle1) / total_delta)), 1)

    memory: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.strip().split()[0]) * 1024
    total = memory.get("MemTotal", 0)
    available = memory.get("MemAvailable", 0)
    disk = shutil.disk_usage("/")
    return {
        "cpu_percent": cpu_percent,
        "cpu_cores": os.cpu_count() or 0,
        "load": [round(value, 2) for value in os.getloadavg()],
        "memory_total": total,
        "memory_used": max(total - available, 0),
        "memory_percent": round(100 * (total - available) / total, 1) if total else 0,
        "disk_total": disk.total,
        "disk_used": disk.used,
        "disk_free": disk.free,
        "disk_percent": round(100 * disk.used / disk.total, 1) if disk.total else 0,
    }


def _modelctl_status() -> dict[str, Any]:
    result = _run([MODELCTL, "status"], timeout=90)
    configured = "unknown"
    detected = "unknown"
    services: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith("configured_mode="):
            configured = line.split("=", 1)[1]
        elif line.startswith("detected_mode="):
            detected = line.split("=", 1)[1]
        elif re.match(r"^[a-z0-9-]+\s+", line):
            name, state = line.split(None, 1)
            services[name] = state
    return {
        "healthy": result.returncode == 0,
        "configured_mode": configured,
        "detected_mode": detected,
        "services": services,
        "message": (result.stderr or result.stdout)[-1200:],
    }


MODE_MODEL_IDS = {
    "copaw": "copaw-flash-9b",
    "bge-m3": "bge-m3",
    "reranker": "bge-reranker-v2-m3",
    "qwen-vl": "qwen3-vl-8b",
    "glm-ocr": "glm-ocr-0.9b",
    "qwen38": "qwen3.8-27b",
    "qwen36": "qwen3.6-35b-a3b",
    "spark-x1": "spark-x1-4b",
    "cosyvoice": "cosyvoice3-0.5b",
}


class ModeSwitchFailure(RuntimeError):
    def __init__(self, message: str, result: dict[str, Any]):
        super().__init__(message)
        self.result = result


def _mode_line_progress(line: str) -> dict[str, Any]:
    start = re.search(r"start one service:\s*([a-z0-9-]+)", line)
    adopt = re.search(r"adopt existing instance:\s*service=([a-z0-9-]+)", line)
    service = (start or adopt).group(1) if start or adopt else ""
    if service:
        model = MODE_MODEL_IDS.get(service, service)
        return {
            "phase": "loading",
            "stage": ("正在加载 " if start else "正在确认现有 ") + model,
            "current_model": model,
        }
    if "switch begin:" in line:
        return {"phase": "switching", "stage": "正在停止原模式模型并准备目标模式"}
    if "switch complete:" in line or "boot reconcile complete:" in line:
        return {"phase": "verification", "stage": "目标容器已就绪，准备实际推理验证"}
    if "switch failed:" in line or "boot reconcile failed" in line:
        return {"phase": "failed", "stage": "目标容器加载失败"}
    return {}


def _stream_modelctl_switch(mode: str, progress: Callable[[dict[str, Any]], None], *,
                            phase: str, started: float, timeout: float = 520) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        [MODELCTL, "switch", mode], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    values: queue.Queue[Any] = queue.Queue()

    def read_output() -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                values.put(line)
        finally:
            values.put(None)

    reader = threading.Thread(target=read_output, daemon=True, name=f"modelctl-{mode}")
    reader.start()
    deadline = time.monotonic() + timeout
    lines: list[str] = []
    latest: dict[str, Any] = {"phase": phase, "stage": f"正在切换到 {mode}"}
    next_tick = 0.0
    ended = False
    while not ended:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise TimeoutError(f"modelctl 切换 {mode} 超过 {int(timeout)} 秒")
        try:
            value = values.get(timeout=min(.5, remaining))
            if value is None:
                ended = True
            else:
                lines.append(value)
                update = _mode_line_progress(value)
                if phase == "rollback" and update:
                    update["phase"] = "rollback"
                    update["stage"] = "回滚：" + update.get("stage", "正在恢复原模式")
                    update["rollback"] = True
                latest.update(update)
                latest["message"] = value.strip()[-500:]
        except queue.Empty:
            pass
        now_value = time.monotonic()
        if now_value >= next_tick:
            progress({**latest, "elapsed_seconds": round(now_value - started, 1)})
            next_tick = now_value + 1
    code = process.wait(timeout=10)
    reader.join(timeout=2)
    output = "".join(lines)[-50_000:]
    return subprocess.CompletedProcess([MODELCTL, "switch", mode], code, output, "")


def _mode_switch_transaction(target: str, progress: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    if RUNTIME_SAFETY is None:
        raise RuntimeError("NPU 安全监控未运行，未执行模式切换")
    safety = RUNTIME_SAFETY.snapshot()
    if safety.get("incident") or safety.get("maintenance") or safety.get("monitor_error"):
        raise RuntimeError("NPU 已隔离、监控未就绪或已有维护任务，未执行模式切换")
    before = _modelctl_status()
    previous = before.get("detected_mode") if before.get("detected_mode") in VALID_MODES else before.get("configured_mode")
    if previous not in VALID_MODES:
        raise RuntimeError("无法确认切换前运行模式，未执行模式切换")
    started = time.monotonic()
    result: dict[str, Any] = {
        "mode": target,
        "previous_mode": previous,
        "verification": None,
        "rollback": None,
    }

    def emit(value: dict[str, Any]) -> None:
        progress({**value, "elapsed_seconds": round(time.monotonic() - started, 1)})

    try:
        emit({"phase": "switching", "stage": f"准备从 {previous} 切换到 {target}"})
        switched = _stream_modelctl_switch(target, progress, phase="switching", started=started)
        result["output"] = switched.stdout[-4000:]
        if switched.returncode:
            raise RuntimeError((switched.stderr or switched.stdout)[-2000:] or "目标模式容器加载失败")
        verifier = Recovery(HOUMO_DEPLOY_DIR, docker=DOCKER, progress=emit, safety=RUNTIME_SAFETY)
        result["verification"] = verifier.verify_current(target)
        emit({"phase": "complete", "stage": "所有目标模型实际推理验证通过，网关已开放调用"})
        return result
    except (Exception, KeyboardInterrupt) as target_error:
        target_result = target_error.result if isinstance(target_error, RecoveryFailure) else None
        result["target_error"] = str(target_error)[:1000]
        result["target_verification"] = target_result
        safety = RUNTIME_SAFETY.snapshot()
        fatal = bool(safety.get("incident") or (target_result or {}).get("first_fault"))
        if not fatal:
            try:
                emit({"phase": "rollback", "stage": f"目标模式验证失败，正在回滚到 {previous}", "rollback": True})
                rolled = _stream_modelctl_switch(previous, progress, phase="rollback", started=started)
                if rolled.returncode:
                    raise RuntimeError((rolled.stderr or rolled.stdout)[-2000:] or "原模式容器恢复失败")
                rollback_verifier = Recovery(HOUMO_DEPLOY_DIR, docker=DOCKER, progress=emit, safety=RUNTIME_SAFETY)
                rollback = rollback_verifier.verify_current(previous)
                result["rollback"] = {"status": "succeeded", "mode": previous, "verification": rollback}
                raise ModeSwitchFailure(
                    f"目标模式 {target} 验证失败；已回滚到 {previous} 并通过实际推理验证",
                    result,
                ) from target_error
            except ModeSwitchFailure:
                raise
            except Exception as rollback_error:
                result["rollback"] = {"status": "failed", "mode": previous, "error": str(rollback_error)[:1000]}
        else:
            result["rollback"] = {"status": "blocked", "mode": previous,
                                  "error": "检测到 NPU fatal，禁止继续加载原模式"}
        emit({"phase": "failed", "stage": "切换或回滚未通过，正在停止同卡模型", "rollback": True})
        stopped = _run([MODELCTL, "stop"], timeout=120)
        result["safe_state"] = "all_device_models_stopped" if stopped.returncode == 0 else "stop_failed_manual_intervention"
        if stopped.returncode:
            result["stop_error"] = (stopped.stderr or stopped.stdout)[-1000:]
        raise ModeSwitchFailure(
            f"目标模式 {target} 验证失败，回滚未通过；同卡模型状态：{result['safe_state']}",
            result,
        ) from target_error


def _startup_verification_transaction(mode: str, progress: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Verify the instances restored by modelctl boot without restarting them."""
    if RUNTIME_SAFETY is None:
        raise RuntimeError("NPU 安全监控未运行，开机验证未执行")
    safety = RUNTIME_SAFETY.snapshot()
    if safety.get("incident"):
        raise RuntimeError("存在尚未恢复的 NPU 故障，开机验证不会自动解除隔离")
    if safety.get("maintenance"):
        raise RuntimeError("已有设备维护任务，开机验证未重复提交")
    if safety.get("monitor_error"):
        raise RuntimeError("NPU 日志监控尚未就绪，开机验证未执行")
    current = _modelctl_status()
    if (not current.get("healthy") or current.get("configured_mode") != mode
            or current.get("detected_mode") != mode):
        raise RuntimeError("当前模式容器尚未完整就绪，保持网关隔离")

    started = time.monotonic()
    verifier = Recovery(HOUMO_DEPLOY_DIR, docker=DOCKER, safety=RUNTIME_SAFETY)
    expected = verifier.selected_names(mode)
    rows = safety.get("services", {})
    if expected and all(
        rows.get(name, {}).get("verified") is True
        and rows[name].get("running") is True
        and rows[name].get("health") == "healthy"
        for name in expected
    ):
        progress({"phase": "complete", "stage": "当前开机实例已经通过实际推理验证",
                  "completed": len(expected), "total": len(expected), "elapsed_seconds": 0.0})
        return {"mode": mode, "status": "succeeded", "already_verified": True,
                "expected_models": expected, "verification": {"status": "succeeded",
                "verified_instances": {name: rows[name].get("identity") for name in expected}}}

    def emit(value: dict[str, Any]) -> None:
        progress({**value, "phase": value.get("phase", "verification"),
                  "elapsed_seconds": round(time.monotonic() - started, 1)})

    emit({"stage": "容器已就绪，开始开机真实推理验证", "completed": 0, "total": len(expected)})
    verifier.progress = emit
    verification = verifier.verify_current(mode)
    emit({"phase": "complete", "stage": "开机真实推理验证通过，网关已开放调用",
          "completed": len(expected), "total": len(expected)})
    return {"mode": mode, "status": "succeeded", "already_verified": False,
            "expected_models": expected, "verification": verification}


def _startup_verification_state() -> dict[str, Any] | None:
    path = HOUMO_DEPLOY_DIR / "state" / "startup-verification.json"
    try:
        if path.is_symlink() or path.stat().st_size > 131_072:
            raise ValueError("invalid startup verification state")
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") != 1 or not isinstance(value.get("boot_id"), str):
            raise ValueError("invalid startup verification state")
        allowed = {
            "schema_version", "boot_id", "source", "mode", "status", "job_id",
            "stage", "current_model", "completed", "total", "elapsed_seconds",
            "started_at", "updated_at", "finished_at", "error", "evidence_id",
            "verified_models", "already_verified",
        }
        result = {key: value[key] for key in allowed if key in value}
        result["current_boot"] = bool(RUNTIME_SAFETY and value["boot_id"] == RUNTIME_SAFETY.boot_id)
        return result
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return {"schema_version": 1, "status": "unknown", "current_boot": False,
                "error": "开机验证状态文件不可读"}


def _job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in job.items() if key not in {"runner"}}


def _start_job(kind: str, target: str, runner: Callable[..., dict[str, Any]], *, with_progress: bool = False) -> dict[str, Any]:
    # Reject duplicates/competing operations, rather than queue a stale restart
    # to execute silently after recovery has finished.
    if not OPERATION_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="另一个管理操作正在执行，请等待完成后再操作")
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "kind": kind,
        "target": target,
        "status": "queued",
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job

    def persist():
        atomic_json(model_api.store_root() / 'jobs' / (job_id + '.json'), _job_snapshot(job))

    try:
        persist()
    except Exception:
        with JOBS_LOCK:
            JOBS.pop(job_id, None)
        OPERATION_LOCK.release()
        raise

    def progress(value):
        job.update(progress=value)
        persist()

    def work() -> None:
        try:
            job["status"] = "running"
            job["started_at"] = _now()
            try:
                persist()
                job["result"] = runner(progress) if with_progress else runner()
                job["status"] = "succeeded"
            except Exception as exc:  # noqa: BLE001 - converted to bounded job error
                job["error"] = f"{type(exc).__name__}: {exc}"[:2000]
                if hasattr(exc, "result"):
                    job["result"] = exc.result
                job["status"] = "failed"
            finally:
                job["finished_at"] = _now()
                persist()
        finally:
            OPERATION_LOCK.release()

    threading.Thread(target=work, daemon=True, name=f"admin-job-{job_id[:8]}").start()
    return _job_snapshot(job)


def _wait_container(service: str, timeout: float = 180) -> None:
    container = CONTAINERS[service]
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        inspected = _docker(
            "inspect",
            "--format",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
            container,
            timeout=8,
        )
        state = inspected.stdout.strip().lower()
        if inspected.returncode == 0 and state in {"healthy", "running"}:
            return
        last_error = (inspected.stderr or inspected.stdout).strip() or f"{container} is not ready"
        time.sleep(2)
    raise RuntimeError(f"{container} did not become healthy: {last_error}")


def _wait_gateway(timeout: float = 80) -> None:
    _wait_container("gateway", timeout)


def _compose_gateway() -> subprocess.CompletedProcess[str]:
    return _docker(
        "compose",
        "--project-directory",
        str(GATEWAY_DIR),
        "-f",
        str(GATEWAY_DIR / "compose.yaml"),
        "up",
        "-d",
        "--force-recreate",
        "gateway",
        timeout=180,
    )




def _compose_sensevoice() -> subprocess.CompletedProcess[str]:
    return _docker(
        "compose",
        "--project-directory",
        str(SENSEVOICE_DIR),
        "-f",
        str(SENSEVOICE_COMPOSE),
        "up",
        "-d",
        "--force-recreate",
        "sensevoice",
        timeout=240,
    )


def _validate_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("base_url must be an http(s) service URL without credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url cannot contain query or fragment")
    if parsed.port is not None and not (1 <= parsed.port <= 65535):
        raise ValueError("base_url port is invalid")
    return value.rstrip("/")


def _read_registry() -> dict[str, Any]:
    return json.loads(MODEL_REGISTRY.read_text(encoding="utf-8"))


def _write_text_atomic(path: Path, text: str) -> None:
    original = path.stat()
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, original.st_mode & 0o7777)
        if hasattr(os, "fchown"):
            os.fchown(fd, original.st_uid, original.st_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _prepare_registry(requested: list[ConfigModel]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    document = _read_registry()
    existing = {str(item["id"]): item for item in document.get("models", [])}
    changes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for proposal in requested:
        if proposal.id in seen:
            raise ValueError(f"duplicate model id: {proposal.id}")
        seen.add(proposal.id)
        current = existing.get(proposal.id)
        if current is None:
            raise ValueError(f"unknown model id: {proposal.id}")
        raw_url = proposal.base_url.strip()
        if proposal.enabled and not raw_url:
            raise ValueError(f"enabled model requires base_url: {proposal.id}")
        normalized_url = _validate_url(raw_url) if raw_url else ""
        new_values: dict[str, Any] = {
            "enabled": proposal.enabled,
            "base_url": normalized_url,
        }
        before = {key: (current.get(key) or "") if key == "base_url" else current.get(key) for key in new_values}
        if before != new_values:
            changes.append({
                "id": proposal.id,
                "group": "models",
                "before": before,
                "after": new_values,
                "restart": ["gateway"],
            })
            current.update(new_values)
    return document, changes


def _prepare_config(
    request: ConfigRequest,
) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]], set[str]]:
    document, model_changes = _prepare_registry(request.models)
    texts, runtime_changes, affected = prepare_runtime_texts(
        request.settings,
        GATEWAY_COMPOSE.read_text(encoding="utf-8"),
        HOUMO_COMPOSE.read_text(encoding="utf-8"),
        SENSEVOICE_COMPOSE.read_text(encoding="utf-8"),
    )
    if model_changes:
        affected.add("gateway")
    return document, texts, runtime_changes + model_changes, affected


def _installed_service_enabled(service: str) -> bool:
    if service == 'gateway':
        return True
    path = HOUMO_DEPLOY_DIR / 'state/model-installations.json'
    if not path.exists():
        return True
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('models'), dict):
        raise ValueError('模型安装状态损坏，不能重启模型服务')
    container = CONTAINERS.get(service)
    for model in Catalog().models.values():
        row = data['models'].get(model['id'], {})
        if not isinstance(row, dict):
            raise ValueError('模型安装状态损坏，不能重启模型服务')
        if model['container'] == container and row.get('installed') is True and row.get('enabled') is True:
            return True
    return False


def _recreate_services(affected: set[str]) -> None:
    operations = {
        "sensevoice": (_compose_sensevoice, lambda: _wait_container("sensevoice", 180)),
        "gateway": (_compose_gateway, _wait_gateway),
    }
    for service in ("sensevoice", "gateway"):
        if service not in affected or not _installed_service_enabled(service):
            continue
        compose, wait = operations[service]
        result = compose()
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout)[-1600:])
        wait()


def _apply_config(request: ConfigRequest) -> dict[str, Any]:
    document, texts, changes, affected = _prepare_config(request)
    if not changes:
        return {"changed": False, "changes": []}

    MODEL_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    backups = MODEL_REGISTRY.parent / "backups"
    backups.mkdir(mode=0o750, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    backup_id = f"{stamp}.{suffix}"
    paths = {
        "registry": MODEL_REGISTRY,
        "gateway": GATEWAY_COMPOSE,
        "houmo": HOUMO_COMPOSE,
        "sensevoice": SENSEVOICE_COMPOSE,
    }
    backup_files = {
        name: backups / f"{name}.{backup_id}{path.suffix}"
        for name, path in paths.items()
    }
    for name, path in paths.items():
        shutil.copy2(path, backup_files[name])
    manifest = backups / f"config.{backup_id}.json"
    manifest.write_text(
        json.dumps(
            {
                "id": backup_id,
                "created_at": _now(),
                "changes": changes,
                "affected_services": sorted(affected),
                "files": {name: backup.name for name, backup in backup_files.items()},
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    try:
        registry_text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        _write_text_atomic(MODEL_REGISTRY, registry_text)
        _write_text_atomic(GATEWAY_COMPOSE, texts["gateway"])
        _write_text_atomic(HOUMO_COMPOSE, texts["houmo"])
        _write_text_atomic(SENSEVOICE_COMPOSE, texts["sensevoice"])
        _recreate_services(affected)
    except Exception:
        for name, path in paths.items():
            shutil.copy2(backup_files[name], path)
        try:
            _recreate_services(affected)
        except Exception:
            pass
        raise
    return {
        "changed": True,
        "changes": changes,
        "affected_services": sorted(affected),
        "backup_id": backup_id,
    }


@app.get("/health/live")
async def live() -> dict[str, Any]:
    return {"ok": bool(AGENT_TOKEN), "service": "modeldeck-agent"}


@app.get('/v1/diagnostics', dependencies=[Depends(require_token)])
async def diagnostics() -> dict[str, Any]:
    async with DIAGNOSTICS_LOCK:
        if DIAGNOSTICS_CACHE and time.monotonic() - DIAGNOSTICS_CACHE['at'] < 15:
            return {**DIAGNOSTICS_CACHE['data'], 'cached': True}
        value = await asyncio.to_thread(collect_diagnostics,
            lambda *args, **kwargs: bounded_command([DOCKER, *args], **kwargs), CONTAINERS, HEALTH_PORTS,
            {'gateway': GATEWAY_COMPOSE, 'houmo': HOUMO_COMPOSE, 'sensevoice': SENSEVOICE_COMPOSE})
        DIAGNOSTICS_CACHE.update(at=time.monotonic(), data=value)
        return {**value, 'cached': False}


@app.get("/v1/status", dependencies=[Depends(require_token)])
async def status() -> dict[str, Any]:
    system, containers, modelctl = await asyncio.gather(
        asyncio.to_thread(_system_metrics),
        asyncio.to_thread(_container_rows),
        asyncio.to_thread(_modelctl_status),
    )
    return {"system": system, "containers": containers, "modelctl": modelctl,
            "runtime_safety": RUNTIME_SAFETY.snapshot() if RUNTIME_SAFETY else None,
            "startup_verification": _startup_verification_state()}


@app.get("/v1/logs", dependencies=[Depends(require_token)])
async def logs(service: str = Query(...), tail: int = Query(default=200, ge=20, le=1000)) -> dict[str, Any]:
    container = CONTAINERS.get(service)
    if container is None:
        raise HTTPException(status_code=400, detail="unknown service")
    result = await asyncio.to_thread(_docker, "logs", "--tail", str(tail), "--timestamps", container, timeout=30)
    text = (result.stdout + result.stderr)[-250_000:]
    return {"service": service, "container": container, "lines": text.splitlines()[-tail:]}


@app.post("/v1/mode-switch", dependencies=[Depends(require_token)])
async def switch_mode(request: ModeRequest) -> dict[str, Any]:
    if request.mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail="invalid mode")

    return _start_job("mode-switch", request.mode,
                      lambda progress: _mode_switch_transaction(request.mode, progress),
                      with_progress=True)


@app.post("/v1/startup-verification", dependencies=[Depends(require_token)])
async def startup_verification(request: ModeRequest) -> dict[str, Any]:
    if request.mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail="invalid mode")
    return _start_job("startup-verification", request.mode,
                      lambda progress: _startup_verification_transaction(request.mode, progress),
                      with_progress=True)


@app.post("/v1/device-recovery", dependencies=[Depends(require_token)])
async def recover_device(request: RecoveryRequest) -> dict[str, Any]:
    if request.mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail="invalid mode")
    return _start_job("device-recovery", "houmo-xh2", lambda progress: Recovery(
        HOUMO_DEPLOY_DIR, docker=DOCKER, progress=progress,
        safety=RUNTIME_SAFETY,
    ).run(request.mode), with_progress=True)


@app.post("/v1/restart/{service}", dependencies=[Depends(require_token)])
async def restart_service(service: str) -> dict[str, Any]:
    container = CONTAINERS.get(service)
    if container is None:
        raise HTTPException(status_code=400, detail="unknown service")
    try:
        if not _installed_service_enabled(service):
            raise ValueError('服务没有已安装且启用的模型，请到模型安装页面启用自测')
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if RUNTIME_SAFETY and container in DEVICE:
        raise HTTPException(status_code=409, detail='同卡模型请使用设备故障恢复，普通重启不能代替实际推理验证')
    allowed_modes = MODE_SERVICES.get(service)
    if allowed_modes:
        modelctl = await asyncio.to_thread(_modelctl_status)
        configured = modelctl["configured_mode"]
        detected = modelctl["detected_mode"]
        if configured not in allowed_modes or (detected in VALID_MODES and detected not in allowed_modes):
            raise HTTPException(
                status_code=409,
                detail=f"service {service} is inactive in mode {configured}",
            )

    def runner(progress: Callable[[dict], None]) -> dict[str, Any]:
        with device_lock(HOUMO_DEPLOY_DIR):
            if not _installed_service_enabled(service):
                raise RuntimeError('安装状态已变化，取消重启')
            if allowed_modes and (HOUMO_DEPLOY_DIR / "state" / "active-mode").read_text().strip() not in allowed_modes:
                raise RuntimeError("运行模式已变化，取消重启")
            progress({"stage": "正在重启 " + service, "steps": []})
            result = _docker("restart", "--time", "30", container, timeout=90)
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout)[-1200:])
            progress({"stage": "等待服务健康检查，尚未确认就绪", "steps": []})
            readiness = _wait_restart_ready(service)
        progress({"stage": "服务重启并通过健康检查（不代替输出质量验收）", "steps": []})
        return {"service": service, "container": container, **readiness}

    return _start_job("restart", service, runner, with_progress=True)


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_token)])
async def job(job_id: str) -> dict[str, Any]:
    with JOBS_LOCK:
        value = JOBS.get(job_id)
        if value is None:
            value = model_api.saved_job(job_id)
        if value is None:
            value = evaluation_api.saved_job(sys.modules[__name__], job_id)
        if value is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _job_snapshot(value)


@app.get("/v1/config", dependencies=[Depends(require_token)])
async def config() -> dict[str, Any]:
    document, settings = await asyncio.gather(
        asyncio.to_thread(_read_registry),
        asyncio.to_thread(
            read_runtime_settings,
            GATEWAY_COMPOSE.read_text(encoding="utf-8"),
            HOUMO_COMPOSE.read_text(encoding="utf-8"),
            SENSEVOICE_COMPOSE.read_text(encoding="utf-8"),
        ),
    )
    services = dict(document.get("services", {}))
    return {
        "services": services,
        "schema": CONFIG_SCHEMA,
        "settings": settings.model_dump(),
        "models": [
            {
                "id": item.get("id"),
                "capability": item.get("capability"),
                "enabled": bool(item.get("enabled")),
                "base_url": item.get("base_url") or "",
                "modes": item.get("modes", []),
            }
            for item in document.get("models", [])
        ],
    }


@app.post("/v1/config/validate", dependencies=[Depends(require_token)])
async def validate_config(request: ConfigRequest) -> dict[str, Any]:
    try:
        _, _, changes, affected = await asyncio.to_thread(_prepare_config, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "valid": True,
        "changes": changes,
        "affected_services": sorted(affected),
        "estimated_downtime_seconds": 20 if affected else 0,
    }


@app.post("/v1/config/apply", dependencies=[Depends(require_token)])
async def apply_config(request: ConfigRequest) -> dict[str, Any]:
    try:
        await asyncio.to_thread(_prepare_config, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    def runner() -> dict[str, Any]:
        with device_lock(HOUMO_DEPLOY_DIR):
            return _apply_config(request)
    return _start_job("config-apply", "runtime-config", runner)


import sys
model_api.register(app, sys.modules[__name__])
from . import ops_api
ops_api.register(app, sys.modules[__name__])
evaluation_api.register(app, sys.modules[__name__])
