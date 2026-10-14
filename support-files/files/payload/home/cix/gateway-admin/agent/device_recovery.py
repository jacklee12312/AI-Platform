"""Allow-listed Houmo lifecycle recovery and fail-fast loading experiments.

No driver reset, reboot, image rebuild, configuration edits or arbitrary commands.
Raw evidence stays on the box with owner-only permissions; API results contain
only summaries. Host CLI can require kernel capture; the container agent cannot.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

try:
    from .runtime_probes import execute as functional_probe
    from .faults import FATAL, fatal_record
except ImportError:  # Standalone host CLI.
    from runtime_probes import execute as functional_probe
    from faults import FATAL, fatal_record

MODE_CONTAINERS = {
    "copaw": "houmo-copaw",
    "spark-x1": "houmo-spark-x1",
    "qwen38": "houmo-qwen38-27b",
    "qwen36": "houmo-qwen36-35b-a3b",
    "qwen-vl": "houmo-qwen3-vl-8b",
    "glm-ocr": "houmo-glm-ocr-0.9b",
    "bge-m3": "houmo-bge-m3",
    "reranker": "houmo-reranker",
    "cosyvoice": "houmo-cosyvoice3",
}
MODEL_BY_CONTAINER = {
    "houmo-copaw": "copaw-flash-9b",
    "houmo-spark-x1": "spark-x1-4b",
    "houmo-qwen38-27b": "qwen3.8-27b",
    "houmo-qwen36-35b-a3b": "qwen3.6-35b-a3b",
    "houmo-qwen3-vl-8b": "qwen3-vl-8b",
    "houmo-glm-ocr-0.9b": "glm-ocr-0.9b",
    "houmo-bge-m3": "bge-m3",
    "houmo-reranker": "bge-reranker-v2-m3",
    "houmo-cosyvoice3": "cosyvoice3-0.5b",
}
DEVICE = tuple(MODE_CONTAINERS.values())


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def next_timestamp(value: str) -> str:
    match = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)\.(\d{1,9})Z$", value)
    if not match:
        return value
    nanos = int(match[2].ljust(9, "0"))
    # Re-reading a boundary is preferable to dropping a log on second rollover.
    return f"{match[1]}.{nanos + 1:09d}Z" if nanos < 999999999 else value


@contextlib.contextmanager
def device_lock(deploy: Path):
    import fcntl  # Linux only; imported lazily so pure unit tests run on Windows.
    with (deploy / "state" / "modelctl.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("设备正被模式切换/恢复/测试占用，请勿重复提交") from exc
        yield


class RecoveryFailure(RuntimeError):
    def __init__(self, message: str, result: dict[str, Any]):
        super().__init__(message)
        self.result = result


class Recovery:
    def __init__(self, deploy: Path, *, docker: str = "/usr/bin/docker", progress: Callable[[dict], None] | None = None,
                 require_kernel: bool = False, safety=None):
        self.deploy, self.docker = deploy, docker
        self.progress = progress or (lambda value: None)
        self.require_kernel = require_kernel
        self.safety = safety
        self.verified_instances = {}
        self.evidence_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self.root = deploy / "state" / "device-recovery" / self.evidence_id
        self.fatal = threading.Event()
        self.closed = threading.Event()
        self.first_fault: dict | None = None
        self.fault_lock = threading.Lock()
        self.monitors: list[threading.Thread] = []
        self.names: list[str] = []
        self.secrets: list[str] = []
        self.stage = "preflight"
        self.result: dict[str, Any] = {"evidence_id": self.evidence_id, "probes": [], "steps": [], "kernel_capture": False}

    def clean(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [REDACTED]", value)

    def save(self, name: str, value: Any) -> None:
        target = self.root / name
        with target.open("w", encoding="utf-8") as stream:
            os.chmod(target, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())

    def step(self, label: str, **progress: Any) -> None:
        self.stage = label
        item = {"at": now(), "stage": label}
        self.result["steps"].append(item)
        self.save("result.json", self.result)
        self.progress({"stage": label, "evidence_id": self.evidence_id,
                       "steps": list(self.result["steps"]), **progress})

    def mark_fault(self, source: str, line: str) -> None:
        with self.fault_lock:
            if self.first_fault is None:
                self.first_fault = {"at": now(), "source": source, "stage": self.stage, "line": self.clean(line[:2000])}
                self.fatal.set()
                self.save("first-fault.json", self.first_fault)

    def check(self) -> None:
        if self.fatal.is_set():
            raise RuntimeError("发现首个底层 fatal 或日志采集故障，已中止继续加载/推理")

    def command(self, args: list[str], timeout: float = 20, *, guarded: bool = True) -> str:
        if guarded:
            self.check()
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + timeout
        try:
            while True:
                if guarded:
                    self.check()
                try:
                    out, err = proc.communicate(timeout=min(.2, max(.01, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"命令超时：{args[1] if len(args) > 1 else 'command'}")
            if proc.returncode:
                raise RuntimeError(self.clean((err or out)[-1200:]))
            return out
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()

    def inspect(self, names: list[str], *, guarded: bool = True) -> list[dict]:
        return json.loads(self.command([self.docker, "inspect", *names], guarded=guarded))

    def snapshot(self, label: str) -> None:
        values = self.inspect(self.names, guarded=False)
        safe = []
        for item in values:
            cmd = item["Config"].get("Cmd", [])
            safe.append({"name": item["Name"], "id": item["Id"], "image_id": item["Image"],
                         "state": {k: v for k, v in item["State"].items() if k != "Health"},
                         "health": item["State"].get("Health", {}).get("Status"),
                         "command": json.loads(self.clean(json.dumps(cmd))),
                         "restart_policy": item["HostConfig"]["RestartPolicy"]})
        self.save(label + "-containers.json", safe)

    def retained_logs(self) -> None:
        # Save every retained line, not a tail. Original historical rotations
        # already deleted by Docker cannot be reconstructed.
        for name in self.names:
            path = self.root / (name + "-before.log")
            with path.open("wb") as stream:
                os.chmod(path, 0o600)
                proc = subprocess.run([self.docker, "logs", "--timestamps", name], stdout=stream, stderr=subprocess.STDOUT, timeout=90)
            if proc.returncode:
                raise RuntimeError(f"保存 {name} 现存完整日志失败；未执行停止操作")

    def monitor(self, name: str, since: str, kernel: bool = False) -> None:
        cursor = since
        kernel_since = datetime.fromisoformat(since).astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")
        path = self.root / (name + "-live.log")
        with path.open("ab", buffering=0) as stream:
            os.chmod(path, 0o600)
            while not self.closed.is_set():
                args = (["journalctl", "-k", "-b", "0", "-f", "--since", kernel_since, "--no-pager", "-o", "short-iso-precise"] if kernel
                        else [self.docker, "logs", "--follow", "--timestamps", "--since", cursor, name])
                proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                def consume() -> None:
                    nonlocal cursor
                    try:
                        assert proc.stdout is not None
                        for raw in iter(proc.stdout.readline, b""):
                            stream.write(raw)
                            line = raw.decode("utf-8", "replace")
                            timestamp = line.split(" ", 1)[0]
                            if not kernel and re.match(r"^\d{4}-.*Z$", timestamp):
                                cursor = next_timestamp(timestamp)
                            if fatal_record(line):
                                self.mark_fault(name, line)
                    except Exception as exc:
                        self.mark_fault("log-capture", type(exc).__name__)
                reader = threading.Thread(target=consume, daemon=True)
                reader.start()
                while proc.poll() is None and not self.closed.wait(.1):
                    pass
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                reader.join(timeout=4)
                if proc.returncode and not self.closed.is_set():
                    self.mark_fault("log-capture", f"{name} collector exited {proc.returncode}")
                    return
                if kernel and not self.closed.is_set():
                    self.mark_fault("log-capture", "kernel collector exited unexpectedly")
                    return
                self.closed.wait(.3)  # Docker follows stop at container exit; reconnect on start.

    def monitored_logs(self, name: str, since: str, kernel: bool = False) -> None:
        try:
            self.monitor(name, since, kernel)
        except Exception as exc:
            self.mark_fault("log-capture", f"{name}: {type(exc).__name__}")

    def close_monitors(self) -> None:
        self.closed.set()
        for thread in self.monitors:
            thread.join(timeout=5)

    def prepare(self, expected_mode: str) -> None:
        mode = (self.deploy / "state" / "active-mode").read_text().strip()
        if mode != expected_mode or mode not in MODE_CONTAINERS:
            raise RuntimeError("运行模式已变化，请刷新页面重新确认")
        present = set(self.command([self.docker, "ps", "-a", "--format", "{{.Names}}"]).splitlines())
        self.names = [name for name in DEVICE if name in present]
        required = set(self.selected_names(mode))
        if not required.issubset(present):
            raise RuntimeError("当前模式的模型容器不完整；恢复不会创建或安装缺失容器")
        self.root.mkdir(parents=True, mode=0o700)
        os.chmod(self.root, 0o700)
        if shutil.disk_usage(self.root).free < 2 * 1024**3:
            raise RuntimeError("证据盘剩余空间不足 2 GiB，未执行恢复")
        for item in self.inspect(self.names):
            for env in item["Config"].get("Env", []):
                key, _, value = env.partition("=")
                if value and any(word in key.upper() for word in ("KEY", "TOKEN", "PASSWORD", "SECRET")):
                    self.secrets.append(value)
        self.step("保存操作前完整现存日志")
        since = now()  # Cover log activity during all of the preflight copies.
        self.snapshot("before")
        self.retained_logs()
        kernel = shutil.which("journalctl") is not None
        if kernel:
            path = self.root / "kernel-before.log"
            with path.open("wb") as stream:
                os.chmod(path, 0o600)
                p = subprocess.run(["journalctl", "-k", "-b", "0", "--no-pager", "-o", "short-iso-precise"], stdout=stream, stderr=subprocess.STDOUT, timeout=30)
            kernel = p.returncode == 0 and path.stat().st_size > 20
        if self.require_kernel and not kernel:
            raise RuntimeError("测试要求完整内核日志，当前无读取权限；未停止模型")
        self.result["kernel_capture"] = kernel
        self.result["capture_since"] = since
        for name in [*self.names, *(["kernel"] if kernel else [])]:
            thread = threading.Thread(target=self.monitored_logs, args=(name, since, name == "kernel"), daemon=True)
            self.monitors.append(thread)
            thread.start()
        self.fatal.wait(.5)
        self.check()

    def stop_all(self) -> None:
        self.command([self.docker, "stop", "--time", "10", *self.names], timeout=50, guarded=False)
        states = self.inspect(self.names, guarded=False)
        if any(i["State"]["Running"] or i["State"]["Restarting"] for i in states):
            raise RuntimeError("同卡模型未全部停止；禁止继续加载")

    def start(self, names: list[str]) -> None:
        self.check()
        self.command([self.docker, "start", *names], timeout=30)

    def wait_ready(self, name: str, timeout: float = 360) -> None:
        if name=='houmo-qwen36-35b-a3b':timeout=max(timeout,600)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            item = self.inspect([name])[0]
            state = item["State"]
            if state.get("Health", {}).get("Status") == "healthy" and state["Running"]:
                return
            if not state["Running"] or state.get("Restarting"):
                raise RuntimeError(f"{name} 加载进程退出/重启，停止测试")
            if self.fatal.wait(.3):
                self.check()
        raise RuntimeError(f"{name} 加载健康检查超时")

    def probe(self, name: str) -> None:
        item = self.inspect([name])[0]
        address = next(v["IPAddress"] for v in item["NetworkSettings"]["Networks"].values() if v["IPAddress"])
        key = next((v.split("=", 1)[1] for v in item["Config"].get("Env", []) if v.startswith("HOUMO_API_KEY=")), "")
        if name not in {'houmo-copaw', 'houmo-qwen38-27b', 'houmo-qwen36-35b-a3b', 'houmo-spark-x1'}:
            values, errors = [], []
            def work_aux():
                try: values.append(functional_probe(name,address,key))
                except Exception as exc: errors.append(exc)
            worker = threading.Thread(target=work_aux,daemon=True); worker.start()
            deadline=time.monotonic()+45
            while worker.is_alive():
                self.check()
                if time.monotonic()>deadline: raise RuntimeError('实际推理验证超时')
                worker.join(.1)
            self.check()
            if errors: raise RuntimeError(self.clean(str(errors[0])))
            self.result['probes'].extend(values)
            self.save('probes.json',self.result['probes'])
            self.record_verified(name,item)
            return
        port, model = {
            'houmo-copaw': (17701, 'copaw-flash-9b'),
            'houmo-qwen38-27b': (17707, 'qwen3.8-27b'),
            'houmo-qwen36-35b-a3b': (17712, 'qwen3.6-35b-a3b'),
            'houmo-spark-x1': (17711, 'spark-x1-4b'),
        }[name]
        probes = [("请计算3+4，只回答数字。", "7", False), ("中国的首都是哪里？只回答城市名。", "北京", False),
                  ("请从1数到20，用空格分隔，不要解释。", "count20", True)]
        for prompt, expected, streaming in probes:
            self.check()
            result: dict = {}
            errors: list[Exception] = []
            def work() -> None:
                try:
                    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0,
                               "max_tokens": 96, "stream": streaming}
                    if streaming:
                        payload["stream_options"] = {"include_usage": True}
                    request = Request(f"http://{address}:{port}/v1/chat/completions", data=json.dumps(payload).encode(),
                                      headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
                    started = time.monotonic()
                    with urlopen(request, timeout=40) as response:
                        if streaming:
                            parts, done, chunks, first = [], False, 0, None
                            for line in response:
                                if self.fatal.is_set():
                                    return
                                if not line.startswith(b"data:"):
                                    continue
                                raw = line[5:].strip()
                                if raw == b"[DONE]":
                                    done = True
                                    continue
                                event = json.loads(raw)
                                if "error" in event:
                                    raise RuntimeError("SSE 返回错误事件")
                                for choice in event.get("choices", []):
                                    content = choice.get("delta", {}).get("content")
                                    if content:
                                        first = first or time.monotonic()
                                        parts.append(content)
                                        chunks += 1
                            content = "".join(parts)
                            if not done or chunks < 2:
                                raise RuntimeError("流式验证缺少 DONE 或实际内容分片")
                            result.update(content_chunks=chunks, ttft_ms=round(((first or started) - started) * 1000, 2))
                        else:
                            body = json.load(response)
                            content = body["choices"][0]["message"]["content"]
                            result["usage"] = body.get("usage")
                    valid = (re.findall(r"\d+", content) == [str(n) for n in range(1, 21)] if expected == "count20"
                             else (re.findall(r"\d+", content) == ["7"] if expected == "7" else "北京" in content))
                    result.update(model=model, test=expected, stream=streaming, content=content[:500],
                                  elapsed_ms=round((time.monotonic() - started) * 1000, 2), passed=valid)
                    if not valid:
                        raise RuntimeError("真实推理结果不符合固定验收答案")
                except Exception as exc:
                    errors.append(exc)
            worker = threading.Thread(target=work, daemon=True)
            worker.start()
            deadline = time.monotonic() + 45
            while worker.is_alive():
                self.check()
                if time.monotonic() > deadline:
                    raise RuntimeError("真实推理验证超过 45 秒，停止继续测试")
                worker.join(.1)
            self.check()
            self.result["probes"].append(dict(result))
            self.save("probes.json", self.result["probes"])
            if errors:
                raise RuntimeError(self.clean(str(errors[0])))
        baseline=json.loads(Path(__file__).with_name('runtime_baseline.json').read_text(encoding='utf-8'))
        qwen38_variant=baseline.get('models',{}).get('qwen3.8-27b',{}).get('hardware_variant')
        if name == 'houmo-qwen36-35b-a3b' or (name == 'houmo-qwen38-27b' and qwen38_variant in ('single', 'dual')):
            self.check()
            vision = functional_probe(name, address, key)
            if not vision.get('passed') or vision.get('test') != 'digit7':
                raise RuntimeError('Qwen3.8 image inference did not pass')
            self.result['probes'].append(vision)
            self.save('probes.json', self.result['probes'])
        self.record_verified(name,item)

    def record_verified(self, name, before):
        after=self.inspect([name])[0]
        def identity(item): return [item['Id'],item['State']['StartedAt'],item.get('RestartCount',0)]
        if identity(after)!=identity(before) or not after['State']['Running']:
            raise RuntimeError('验证期间模型实例变化，不确认恢复')
        self.verified_instances[name]=identity(after)

    def selected_names(self, mode):
        if mode not in MODE_CONTAINERS:
            raise ValueError('invalid mode')
        defaults = (MODE_CONTAINERS[mode],)
        if mode == 'cosyvoice':
            # The pinned HLIEvLLM bundle is provisioned as one runtime unit and
            # is not represented by the GGUF-only model installer state.
            return defaults
        path = self.deploy / 'state/model-installations.json'
        if not path.exists():
            return defaults
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('models'), dict):
            raise ValueError('安装状态损坏，不能恢复模型')
        selected = [name for name in defaults if data['models'].get(MODEL_BY_CONTAINER[name], {}).get('installed') is True
                    and data['models'].get(MODEL_BY_CONTAINER[name], {}).get('enabled') is True]
        if not selected:
            raise ValueError('当前模式没有已安装且启用的后摩模型，保留设备隔离；请由维护人员检查设备与安装事务')
        return selected

    def verify_current(self, mode: str) -> dict:
        """Verify every currently running target instance without restarting it.

        This is used after ``modelctl switch``.  Container health alone does not
        reopen gateway admission: fixed inference probes, stable identities and
        a final retained-log scan must all pass first.
        """
        if mode not in MODE_CONTAINERS:
            raise ValueError("invalid mode")
        with device_lock(self.deploy):
            selected = self.selected_names(mode)
            safety_token = None
            self.result.update(mode=mode, plan="verify-current", started_at=now())
            try:
                self.prepare(mode)
                inspected = self.inspect(self.names)
                running = {item["Name"].lstrip("/") for item in inspected if item["State"].get("Running")}
                expected = set(selected)
                if running != expected:
                    missing = sorted(expected - running)
                    unexpected = sorted(running - expected)
                    raise RuntimeError(f"目标模式容器不一致：缺少={missing or '无'}，额外运行={unexpected or '无'}")
                total = len(selected)
                completed = 0
                for name in selected:
                    self.step("验证 " + name + " 实际推理", phase="verification",
                              current_model=name, completed=completed, total=total)
                    self.wait_ready(name)
                    self.probe(name)
                    completed += 1
                self.check()
                self.snapshot("after")
                self.close_monitors()
                self.check()
                if self.safety:
                    # Open the short maintenance window only after probes pass.
                    # This keeps rollback possible when a functional probe fails,
                    # while finish_recovery still scans every retained log and
                    # rejects a changed instance or a new fatal.
                    safety_token = self.safety.begin_recovery()
                    self.safety.finish_recovery(safety_token, self.verified_instances)
                self.result.update(status="succeeded", verified_instances=self.verified_instances)
                self.step("实际推理验证完成，已开放网关调用", phase="complete",
                          completed=completed, total=total)
            except (Exception, KeyboardInterrupt) as exc:
                self.result.update(status="failed", error=self.clean(str(exc)),
                                   failed_stage=self.stage, first_fault=self.first_fault)
                if self.root.exists():
                    try:
                        self.snapshot("fault")
                    except Exception as snapshot_error:
                        self.result["snapshot_error"] = self.clean(str(snapshot_error))
                raise RecoveryFailure(self.clean(str(exc)), self.result) from exc
            finally:
                if self.safety and safety_token and self.result.get("status") != "succeeded":
                    self.safety.abort_recovery(safety_token)
                self.close_monitors()
                self.result["finished_at"] = now()
                if self.root.exists():
                    self.save("result.json", self.result)
            return self.result

    def run(self, mode: str, plan: str = "recover") -> dict:
        if plan not in {"recover", "baseline", "parallel"} or mode not in MODE_CONTAINERS:
            raise ValueError("invalid fixed recovery plan")
        with device_lock(self.deploy):
            selected = self.selected_names(mode)
            if (self.deploy / 'state/model-installations.json').exists() and plan != 'recover':
                raise ValueError('按需部署仅支持恢复已启用模型，不支持全量加载对照实验')
            changed = False
            safety_token = None
            self.result.update(mode=mode, plan=plan, started_at=now())
            try:
                if self.safety: safety_token=self.safety.begin_recovery()
                self.prepare(mode)
                self.step("停止全部同卡模型")
                changed = True
                self.stop_all()
                self.check()
                self.step("确认同卡模型均已停止")
                primary = selected[0]
                initial = [primary]
                self.step("目标模型单独干净加载")
                started = time.monotonic()
                self.start(initial)
                for name in initial:
                    self.wait_ready(name)
                self.result["load_seconds"] = round(time.monotonic() - started, 3)
                self.step("验证主模型非流式与流式真实推理")
                self.probe(primary)
                self.check()
                self.snapshot("after")
                self.close_monitors()
                self.check()  # Drain log readers before declaring success.
                if self.safety: self.safety.finish_recovery(safety_token,self.verified_instances)
                self.result["status"] = "succeeded"
                self.result['verified_instances']=self.verified_instances
                self.step("完成（当前独占模型已验证实际推理；不代替完整质量验收）")
            except (Exception, KeyboardInterrupt) as exc:
                self.result.update(status="failed", error=self.clean(str(exc)), failed_stage=self.stage, first_fault=self.first_fault)
                # First-fault evidence is already durably written by the reader.
                # Stop submissions/processes before slower full-state inspection.
                if changed:
                    try:
                        self.stop_all()
                        self.result["safe_state"] = "all_device_models_stopped"
                    except Exception as stop_error:
                        self.result["safe_state"] = "stop_failed_manual_intervention"
                        self.result["stop_error"] = self.clean(str(stop_error))
                if self.root.exists():
                    # Do not auto-reset or restart after failure; retain fault and stop.
                    try:
                        self.snapshot("fault")
                    except Exception as snapshot_error:
                        self.result["snapshot_error"] = self.clean(str(snapshot_error))
                raise RecoveryFailure(self.clean(str(exc)), self.result) from exc
            finally:
                if self.safety and safety_token and self.result.get('status')!='succeeded':
                    self.safety.abort_recovery(safety_token)
                self.close_monitors()
                self.result["finished_at"] = now()
                if self.root.exists():
                    self.save("result.json", self.result)
            return self.result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--deploy-dir", type=Path, default=Path("/home/cix/houmo-docker-deployment"))
    parser.add_argument("--plan", choices=("baseline", "parallel", "recover"), required=True)
    parser.add_argument("--mode", choices=tuple(MODE_CONTAINERS), default="copaw")
    parser.add_argument("--require-kernel-logs", action="store_true")
    args = parser.parse_args()
    engine = Recovery(args.deploy_dir, require_kernel=args.require_kernel_logs,
                      progress=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))
    try:
        print(json.dumps(engine.run(args.mode, args.plan), ensure_ascii=False), flush=True)
    except RecoveryFailure as exc:
        print(json.dumps(exc.result, ensure_ascii=False), flush=True)
        raise SystemExit(1)
