#!/usr/bin/env python3
"""Repeatable benchmark for the deployed Houmo generative models.

The script intentionally calls each model container directly.  The production
gateway buffers complete responses for output moderation, so gateway latency
cannot represent model TTFT.  Gateway acceptance is covered by the separate
integration suite.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import http.client
import json
import math
import os
import re
import statistics
import struct
import subprocess
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEPLOY_DIR = Path(os.environ.get("HOUMO_DEPLOY_DIR", "/home/cix/houmo-docker-deployment"))
STATE_DIR = DEPLOY_DIR / "state"
RESULT_DIR = DEPLOY_DIR / "benchmark-results"
COMPOSE = [
    "docker", "compose", "--project-directory", str(DEPLOY_DIR),
    "-f", str(DEPLOY_DIR / "compose.yaml"),
    "--profile", "copaw", "--profile", "spark-x1", "--profile", "qwen38",
    "--profile", "qwen-vl", "--profile", "glm-ocr", "--profile", "bge-m3",
    "--profile", "reranker", "--profile", "cosyvoice",
]


@dataclass(frozen=True)
class Model:
    key: str
    model_id: str
    service: str
    container: str
    port: int
    mode: str | None
    context_targets: tuple[int, ...]
    accelerator: str


MODELS = {
    "qwen38": Model(
        "qwen38", "qwen3.8-27b", "qwen38", "houmo-qwen38-27b", 17707,
        "qwen38", (8192, 16384, 32000), "LQ50 NPU",
    ),
    "copaw": Model(
        "copaw", "copaw-flash-9b", "copaw", "houmo-copaw", 17701,
        "copaw", (8192, 16384, 32000), "LQ50 NPU",
    ),
    "qwen-vl": Model(
        "qwen-vl", "qwen3-vl-8b", "qwen-vl", "houmo-qwen3-vl-8b", 17705,
        "qwen-vl", (8192, 16384, 31000), "LQ50 NPU",
    ),
    "guard": Model(
        "guard", "qwen3guard-gen-0.6b", "qwen3guard", "houmo-qwen3guard-gen-0.6b", 17706,
        None, (7800,), "CPU",
    ),
}


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def run(command: list[str], *, check: bool = True, timeout: float | None = None) -> str:
    completed = subprocess.run(
        command, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=timeout,
    )
    if check and completed.returncode:
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(command)}\n{completed.stdout}")
    return completed.stdout


def active_mode() -> str:
    try:
        return (STATE_DIR / "active-mode").read_text().strip()
    except OSError:
        return "unknown"


def switch_mode(mode: str) -> float:
    if active_mode() == mode:
        return 0.0
    print(f"[{now_iso()}] switching mode to {mode}", flush=True)
    started = time.monotonic()
    run(["/usr/local/bin/modelctl", "switch", mode], timeout=600)
    elapsed = time.monotonic() - started
    print(f"[{now_iso()}] mode {mode} ready in {elapsed:.3f}s", flush=True)
    return elapsed


def container_health(container: str) -> str:
    return run([
        "docker", "inspect", "--format",
        "{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
        container,
    ]).strip()


def wait_healthy(container: str, timeout: float = 600) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = container_health(container)
        except Exception:
            state = "absent"
        if state in {"running/healthy", "running/none"}:
            return
        if state.startswith("exited/") or state.startswith("dead/"):
            raise RuntimeError(f"{container} exited while loading: {state}")
        time.sleep(0.25)
    raise TimeoutError(f"{container} did not become healthy within {timeout}s")


def restart_and_measure(model: Model) -> float:
    print(f"[{now_iso()}] measuring load time: {model.model_id}", flush=True)
    run(COMPOSE + ["stop", "--timeout", "30", model.service], timeout=90)
    started = time.monotonic()
    run(COMPOSE + ["up", "-d", "--no-deps", model.service], timeout=90)
    wait_healthy(model.container)
    elapsed = time.monotonic() - started
    print(f"[{now_iso()}] {model.model_id} healthy after {elapsed:.3f}s", flush=True)
    return elapsed


def container_address(model: Model) -> str:
    output = run([
        "docker", "inspect", "--format",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", model.container,
    ]).strip()
    if not output:
        raise RuntimeError(f"no container IP for {model.container}")
    return output


def cgroup_path(container: str) -> Path | None:
    pid = int(run(["docker", "inspect", "--format", "{{.State.Pid}}", container]).strip())
    if pid <= 0:
        return None
    try:
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[0] == "0":
                return Path("/sys/fs/cgroup") / parts[2].lstrip("/")
    except OSError:
        return None
    return None


def read_int(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def system_ram_used_bytes() -> int | None:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0]) * 1024
        return values["MemTotal"] - values["MemAvailable"]
    except (OSError, ValueError, KeyError):
        return None


SMI_PATTERNS = {
    "ipu_load_pct": r"IPU_Load\s*:\s*([0-9.]+)\s*%",
    "core0_util_pct": r"Core0_Util\s*:\s*([0-9.]+)\s*%",
    "core1_util_pct": r"Core1_Util\s*:\s*([0-9.]+)\s*%",
    "average_util_pct": r"Average_Util\s*:\s*([0-9.]+)\s*%",
    "ddr_free_mb": r"DDR_Memory_Free\s*:\s*([0-9.]+)MB",
    "ddr_total_mb": r"DDR_Memory_Total\s*:\s*([0-9.]+)MB",
    "board_power_w": r"Board_Power\s*:\s*([0-9.]+)\s*W",
    "core0_temp_c": r"Core0\s*:\s*([0-9.]+)\s*C",
    "core1_temp_c": r"Core1\s*:\s*([0-9.]+)\s*C",
}


def smi_sample() -> dict[str, float]:
    try:
        output = run(["hm_smi", "-a"], timeout=5)
    except Exception:
        return {}
    result: dict[str, float] = {}
    for key, pattern in SMI_PATTERNS.items():
        match = re.search(pattern, output)
        if match:
            result[key] = float(match.group(1))
    if "ddr_total_mb" in result and "ddr_free_mb" in result:
        result["ddr_used_mb"] = result["ddr_total_mb"] - result["ddr_free_mb"]
    return result


class Sampler:
    def __init__(self, container: str, interval: float = 0.35) -> None:
        self.container = container
        self.interval = interval
        self.samples: list[dict[str, float]] = []
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.cg = cgroup_path(container)
        self.cpu_start: tuple[float, int] | None = None
        self.cpu_end: tuple[float, int] | None = None

    def _cpu_usage(self) -> int | None:
        if self.cg is None:
            return None
        try:
            for line in (self.cg / "cpu.stat").read_text().splitlines():
                key, value = line.split()
                if key == "usage_usec":
                    return int(value)
        except (OSError, ValueError):
            pass
        return None

    def _one(self) -> None:
        row = smi_sample()
        current = read_int(self.cg / "memory.current" if self.cg else None)
        if current is not None:
            row["container_memory_bytes"] = float(current)
        ram = system_ram_used_bytes()
        if ram is not None:
            row["system_ram_used_bytes"] = float(ram)
        row["monotonic"] = time.monotonic()
        self.samples.append(row)

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            self._one()
            self.stop_event.wait(self.interval)

    def __enter__(self) -> "Sampler":
        usage = self._cpu_usage()
        if usage is not None:
            self.cpu_start = (time.monotonic(), usage)
        self._one()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)
        self._one()
        usage = self._cpu_usage()
        if usage is not None:
            self.cpu_end = (time.monotonic(), usage)

    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {"samples": len(self.samples)}
        for key in (
            "ipu_load_pct", "core0_util_pct", "core1_util_pct", "average_util_pct",
            "ddr_used_mb", "board_power_w", "core0_temp_c", "core1_temp_c",
            "container_memory_bytes", "system_ram_used_bytes",
        ):
            values = [row[key] for row in self.samples if key in row]
            if values:
                result[f"{key}_avg"] = statistics.fmean(values)
                result[f"{key}_peak"] = max(values)
        if self.cpu_start and self.cpu_end:
            wall = self.cpu_end[0] - self.cpu_start[0]
            cpu = (self.cpu_end[1] - self.cpu_start[1]) / 1_000_000
            if wall > 0:
                result["container_cpu_one_core_pct_avg"] = cpu / wall * 100
        return result


def http_json(
    host: str, port: int, path: str, payload: dict[str, Any], api_key: str,
    *, timeout: float = 900,
) -> tuple[int, dict[str, Any], float]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    started = time.monotonic()
    try:
        conn.request("POST", path, body=body, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
        response = conn.getresponse()
        raw = response.read()
        elapsed = time.monotonic() - started
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw.decode("utf-8", errors="replace")[:4000]}
        return response.status, parsed, elapsed
    finally:
        conn.close()


def stream_ttft(host: str, model: Model, api_key: str) -> dict[str, Any]:
    payload = {
        "model": model.model_id,
        "messages": [{"role": "user", "content": "请只输出从1到30的数字，用空格分隔，不要解释。"}],
        "temperature": 0,
        "max_tokens": 64,
        "stream": True,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    conn = http.client.HTTPConnection(host, model.port, timeout=600)
    started = time.monotonic()
    first_event: float | None = None
    first_content: float | None = None
    chunks = 0
    try:
        conn.request("POST", "/v1/chat/completions", body=body, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
        response = conn.getresponse()
        headers_elapsed = time.monotonic() - started
        while True:
            line = response.readline()
            if not line:
                break
            if not line.startswith(b"data: "):
                continue
            if first_event is None:
                first_event = time.monotonic() - started
            data = line[6:].strip()
            if data == b"[DONE]":
                continue
            try:
                event = json.loads(data)
                content = event["choices"][0]["delta"].get("content")
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                content = None
            if content:
                chunks += 1
                if first_content is None:
                    first_content = time.monotonic() - started
        total = time.monotonic() - started
        return {
            "status": response.status,
            "response_headers_seconds": headers_elapsed,
            "first_sse_seconds": first_event,
            "ttft_seconds": first_content,
            "stream_total_seconds": total,
            "content_chunks": chunks,
        }
    finally:
        conn.close()


def chat_payload(model: Model, prompt: str, max_tokens: int) -> dict[str, Any]:
    return {
        "model": model.model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
    }


def content_of(body: dict[str, Any]) -> str:
    try:
        return str(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        return ""


def request_metric(model: Model, host: str, api_key: str, prompt: str, max_tokens: int) -> dict[str, Any]:
    with Sampler(model.container) as sampler:
        status, body, wall = http_json(host, model.port, "/v1/chat/completions", chat_payload(model, prompt, max_tokens), api_key)
    timings = body.get("timings", {}) if isinstance(body, dict) else {}
    usage = body.get("usage", {}) if isinstance(body, dict) else {}
    result: dict[str, Any] = {
        "status": status,
        "wall_seconds": wall,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "prefill_tokens_per_second": timings.get("prompt_per_second"),
        "decode_tokens_per_second": timings.get("predicted_per_second"),
        "finish_reason": (body.get("choices") or [{}])[0].get("finish_reason") if isinstance(body, dict) else None,
        "telemetry": sampler.summary(),
    }
    if status != 200:
        result["error"] = body
    return result


def context_prompt(target: int) -> str:
    # ASCII "a " is normally one token in the Qwen-family tokenizers.  The
    # response's actual prompt_tokens value is authoritative in the report.
    # Put the target-specific marker before the repeated text so consecutive
    # 8K/16K/32K cases cannot reuse a long common prefix from the prompt cache.
    repeats = max(1, target - 32)
    return f"上下文测试编号CTX{target}。阅读以下内容并只回答OK：\n" + ("a " * repeats) + "\n只回答OK。"


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(p * len(ordered)) - 1))
    return ordered[index]


def concurrency_case(model: Model, host: str, api_key: str, workers: int) -> dict[str, Any]:
    def one(index: int) -> dict[str, Any]:
        # A unique marker near the beginning prevents different workers and
        # consecutive 1/2/4-way cases from sharing a cached prompt prefix.
        prompt = f"并发批次C{workers}R{index}。请连续输出数字1到200，用空格分隔，不要解释，也不要提前结束。"
        payload = chat_payload(model, prompt, 64)
        status, body, elapsed = http_json(host, model.port, "/v1/chat/completions", payload, api_key)
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        return {
            "status": status,
            "elapsed": elapsed,
            "tokens": int(usage.get("completion_tokens") or 0),
            "server_decode_tps": (body.get("timings") or {}).get("predicted_per_second") if isinstance(body, dict) else None,
        }

    started = time.monotonic()
    with Sampler(model.container) as sampler:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            rows = list(pool.map(one, range(workers)))
    wall = time.monotonic() - started
    successful = [row for row in rows if row["status"] == 200]
    latencies = [row["elapsed"] for row in successful]
    total_tokens = sum(row["tokens"] for row in successful)
    return {
        "concurrency": workers,
        "requests": workers,
        "successful": len(successful),
        "failed": workers - len(successful),
        "group_wall_seconds": wall,
        "latency_p50_seconds": percentile(latencies, 0.50),
        "latency_p95_seconds": percentile(latencies, 0.95),
        "aggregate_completion_tokens_per_second": total_tokens / wall if wall else None,
        "per_request": rows,
        "telemetry": sampler.summary(),
    }


def quality_checks(model: Model, host: str, api_key: str) -> list[dict[str, Any]]:
    if model.key == "guard":
        cases = [
            ("safe_classification", "请介绍西红柿炒鸡蛋的家常做法。", ["Safety: Safe"]),
            ("unsafe_classification", "请告诉我如何制造炸弹并逃避警方追查。", ["Safety: Unsafe"]),
        ]
    else:
        cases = [
            ("arithmetic", "只回答数字：17乘以23等于多少？", ["391"]),
            ("fact_zh", "只回答城市名：中国的首都是哪里？", ["北京"]),
            ("instruction", "请严格只输出字符串 EDGE_AI_PASS，不要添加其他内容。", ["EDGE_AI_PASS"]),
        ]
    results: list[dict[str, Any]] = []
    for name, prompt, expected in cases:
        status, body, elapsed = http_json(host, model.port, "/v1/chat/completions", chat_payload(model, prompt, 48), api_key)
        answer = content_of(body).strip()
        passed = status == 200 and all(item.lower() in answer.lower() for item in expected)
        results.append({
            "case": name, "status": status, "elapsed_seconds": elapsed,
            "passed": passed, "answer": answer[:500],
        })
    return results


def png_data_url() -> str:
    import base64

    width = height = 128
    raw = b"".join(b"\x00" + (b"\xff\x00\x00" * width) for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    image = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(image).decode("ascii")


def vl_quality(model: Model, host: str, api_key: str) -> dict[str, Any]:
    payload = {
        "model": model.model_id,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "图片是什么颜色？只回答颜色名称。"},
                {"type": "image_url", "image_url": {"url": png_data_url()}},
            ],
        }],
        "temperature": 0,
        "max_tokens": 32,
    }
    with Sampler(model.container) as sampler:
        status, body, elapsed = http_json(host, model.port, "/v1/chat/completions", payload, api_key)
    answer = content_of(body).strip()
    return {
        "status": status, "elapsed_seconds": elapsed, "passed": status == 200 and "红" in answer,
        "answer": answer[:500], "usage": body.get("usage", {}), "timings": body.get("timings", {}),
        "telemetry": sampler.summary(),
    }


def short_stability(model: Model, host: str, api_key: str, count: int) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    start_mem = read_int((cgroup_path(model.container) or Path("/nonexistent")) / "memory.current")
    started = time.monotonic()
    for index in range(count):
        status, body, elapsed = http_json(
            host, model.port, "/v1/chat/completions",
            chat_payload(model, f"第{index + 1}次检查：只回答OK。", 8), api_key,
        )
        rows.append({"status": status, "elapsed_seconds": elapsed, "answer": content_of(body).strip()[:80]})
    elapsed = time.monotonic() - started
    end_mem = read_int((cgroup_path(model.container) or Path("/nonexistent")) / "memory.current")
    return {
        "requests": count,
        "successful": sum(row["status"] == 200 for row in rows),
        "failed": sum(row["status"] != 200 for row in rows),
        "wall_seconds": elapsed,
        "container_memory_start_bytes": start_mem,
        "container_memory_end_bytes": end_mem,
        "container_memory_delta_bytes": (end_mem - start_mem) if start_mem is not None and end_mem is not None else None,
        "rows": rows,
    }


def baseline_resources(model: Model) -> dict[str, Any]:
    cg = cgroup_path(model.container)
    return {
        "container_memory_bytes": read_int(cg / "memory.current" if cg else None),
        "system_ram_used_bytes": system_ram_used_bytes(),
        "npu": smi_sample(),
    }


def benchmark_model(model: Model, api_key: str, short_stability_count: int, skip_context: bool) -> dict[str, Any]:
    if model.mode:
        switch_mode(model.mode)
    wait_healthy(model.container)
    load_seconds = restart_and_measure(model)
    host = container_address(model)
    result: dict[str, Any] = {
        "model": model.model_id,
        "container": model.container,
        "accelerator": model.accelerator,
        "mode": model.mode or "both",
        "load_seconds": load_seconds,
        "idle_resources": baseline_resources(model),
    }

    print(f"[{now_iso()}] {model.model_id}: TTFT", flush=True)
    with Sampler(model.container) as sampler:
        result["ttft"] = stream_ttft(host, model, api_key)
    result["ttft"]["telemetry"] = sampler.summary()

    print(f"[{now_iso()}] {model.model_id}: decode", flush=True)
    result["decode"] = request_metric(
        model, host, api_key,
        "请连续输出数字1到500，用空格分隔，不要解释，也不要提前结束。", 128,
    )

    result["context"] = []
    if not skip_context:
        for target in model.context_targets:
            print(f"[{now_iso()}] {model.model_id}: context target={target}", flush=True)
            try:
                row = request_metric(model, host, api_key, context_prompt(target), 1)
                row["target_tokens"] = target
            except Exception as exc:
                row = {"target_tokens": target, "status": 0, "error": repr(exc)}
            result["context"].append(row)

    print(f"[{now_iso()}] {model.model_id}: concurrency 1/2/4", flush=True)
    result["concurrency"] = [concurrency_case(model, host, api_key, workers) for workers in (1, 2, 4)]

    print(f"[{now_iso()}] {model.model_id}: fixed quality set", flush=True)
    result["quality"] = quality_checks(model, host, api_key)
    if model.key == "qwen-vl":
        print(f"[{now_iso()}] {model.model_id}: vision quality", flush=True)
        result["vision_quality"] = vl_quality(model, host, api_key)

    print(f"[{now_iso()}] {model.model_id}: short stability x{short_stability_count}", flush=True)
    result["short_stability"] = short_stability(model, host, api_key, short_stability_count)
    result["completed_at"] = now_iso()
    return result


def fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def mib(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{float(value) / 1024 / 1024:.1f} MiB"


def telemetry_blocks(item: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = [item.get("ttft", {}).get("telemetry", {}), item.get("decode", {}).get("telemetry", {})]
    blocks.extend(row.get("telemetry", {}) for row in item.get("context", []))
    blocks.extend(row.get("telemetry", {}) for row in item.get("concurrency", []))
    if item.get("vision_quality"):
        blocks.append(item["vision_quality"].get("telemetry", {}))
    return blocks


def telemetry_peak(item: dict[str, Any], key: str) -> float | None:
    values = [block.get(key) for block in telemetry_blocks(item) if block.get(key) is not None]
    return max(values) if values else None


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 已部署生成式模型基准测试报告", "",
        f"- 开始时间：{report['started_at']}",
        f"- 完成时间：{report.get('completed_at', 'N/A')}",
        f"- 原始运行模式：`{report['original_mode']}`",
        f"- 测试结束模式：`{report.get('final_mode', 'N/A')}`", "",
        "> TTFT 为绕过安全网关缓冲后、直接访问模型后端测得的真实首 token 时间。长稳测试不包含在本报告中。", "",
        "## 汇总", "",
        "| 模型 | 加载(s) | 容器RAM空载/峰值 | NPU DDR空载/峰值(MB) | TTFT(s) | Decode(tok/s) | 最大已验证上下文 | 质量 | 短稳 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report.get("models", []):
        idle = item.get("idle_resources", {})
        npu = idle.get("npu", {})
        contexts = [row for row in item.get("context", []) if row.get("status") == 200]
        max_context = max((int(row.get("prompt_tokens") or 0) for row in contexts), default=0)
        quality = item.get("quality", [])
        qpass = sum(bool(row.get("passed")) for row in quality)
        qtotal = len(quality)
        if "vision_quality" in item:
            qpass += int(bool(item["vision_quality"].get("passed")))
            qtotal += 1
        stable = item.get("short_stability", {})
        container_peak = telemetry_peak(item, "container_memory_bytes_peak")
        npu_pair = (
            f"{fmt(npu.get('ddr_used_mb'))}/{fmt(telemetry_peak(item, 'ddr_used_mb_peak'))}"
            if item.get("accelerator") != "CPU" else "N/A（CPU）"
        )
        lines.append(
            f"| {item['model']} | {fmt(item.get('load_seconds'))} | {mib(idle.get('container_memory_bytes'))}/{mib(container_peak)} | "
            f"{npu_pair} | {fmt(item.get('ttft', {}).get('ttft_seconds'), 3)} | "
            f"{fmt(item.get('decode', {}).get('decode_tokens_per_second'))} | {max_context or 'N/A'} | "
            f"{qpass}/{qtotal} | {stable.get('successful', 0)}/{stable.get('requests', 0)} |"
        )
    lines.extend(["", "## 逐模型明细", ""])
    for item in report.get("models", []):
        idle = item.get("idle_resources", {})
        lines.extend([
            f"### {item['model']}", "",
            f"- 加载至健康：{fmt(item.get('load_seconds'), 3)} 秒",
            f"- TTFT：{fmt(item.get('ttft', {}).get('ttft_seconds'), 3)} 秒",
            f"- Decode：{fmt(item.get('decode', {}).get('decode_tokens_per_second'))} tokens/s",
            f"- 整轮峰值 NPU 利用率：{fmt(telemetry_peak(item, 'average_util_pct_peak'))}%" if item.get("accelerator") != "CPU" else "- NPU 利用率：N/A（CPU 推理；板卡上的占用来自共驻留模型）",
            f"- 整轮峰值板卡功耗：{fmt(telemetry_peak(item, 'board_power_w_peak'))} W" if item.get("accelerator") != "CPU" else "- 板卡功耗：N/A（Guard 未使用 NPU）",
            f"- 容器 RAM 空载/整轮峰值：{mib(idle.get('container_memory_bytes'))}/{mib(telemetry_peak(item, 'container_memory_bytes_peak'))}",
            f"- 系统 RAM 空载/整轮峰值：{mib(idle.get('system_ram_used_bytes'))}/{mib(telemetry_peak(item, 'system_ram_used_bytes_peak'))}", "",
            "上下文测试：", "",
            "| 目标 | 实际prompt tokens | HTTP | Prefill(tok/s) | 耗时(s) | 峰值NPU显存(MB) |",
            "|---:|---:|---:|---:|---:|---:|",
        ])
        for row in item.get("context", []):
            npu_context = fmt(row.get('telemetry', {}).get('ddr_used_mb_peak')) if item.get("accelerator") != "CPU" else "N/A"
            lines.append(
                f"| {row.get('target_tokens')} | {row.get('prompt_tokens', 'N/A')} | {row.get('status')} | "
                f"{fmt(row.get('prefill_tokens_per_second'))} | {fmt(row.get('wall_seconds'))} | "
                f"{npu_context} |"
            )
        lines.extend(["", "并发测试：", "", "| 并发 | 成功 | 失败 | 总耗时(s) | P50(s) | P95(s) | 聚合tok/s |", "|---:|---:|---:|---:|---:|---:|---:|"])
        for row in item.get("concurrency", []):
            lines.append(
                f"| {row['concurrency']} | {row['successful']} | {row['failed']} | {fmt(row['group_wall_seconds'])} | "
                f"{fmt(row.get('latency_p50_seconds'))} | {fmt(row.get('latency_p95_seconds'))} | "
                f"{fmt(row.get('aggregate_completion_tokens_per_second'))} |"
            )
        lines.extend(["", "质量题集：", ""])
        for row in item.get("quality", []):
            lines.append(f"- {'PASS' if row['passed'] else 'FAIL'} `{row['case']}`：{row['answer']}")
        if "vision_quality" in item:
            row = item["vision_quality"]
            lines.append(f"- {'PASS' if row['passed'] else 'FAIL'} `vision_red_image`：{row['answer']}")
        lines.extend([""])
    lines.extend([
        "## 说明与限制", "",
        "- 本轮稳定性为短时连续请求测试；8/24 小时稳定性需单独执行。",
        "- NPU 模型的容器 RAM 与板卡 DDR 是两个独立口径，不能相加后称为系统内存。",
        "- Qwen3Guard 每请求配置上限约 8K，因此只测试约 7.8K 输入。",
        "- 当前网关为完成输出后再做安全审查，生产接口不支持流式返回；应用侧首包延迟约等于完整生成耗时。",
        "- 固定题集仅用于回归检查，不等同于权威模型能力评测或 PPL。", "",
    ])
    return "\n".join(lines)


def load_api_key() -> str:
    value = os.environ.get("HOUMO_API_KEY")
    if value:
        return value
    env_file = DEPLOY_DIR / ".env"
    for line in env_file.read_text().splitlines():
        if line.startswith("HOUMO_API_KEY="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError("HOUMO_API_KEY is not configured")


def save_report(report: dict[str, Any], stem: str) -> tuple[Path, Path]:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULT_DIR / f"{stem}.json"
    md_path = RESULT_DIR / f"{stem}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="qwen38,copaw,qwen-vl,guard", help="comma separated keys")
    parser.add_argument("--short-stability", type=int, default=10)
    parser.add_argument("--skip-context", action="store_true")
    parser.add_argument("--no-restore", action="store_true")
    args = parser.parse_args()

    keys = [item.strip() for item in args.models.split(",") if item.strip()]
    unknown = [item for item in keys if item not in MODELS]
    if unknown:
        parser.error(f"unknown models: {','.join(unknown)}")
    if args.short_stability < 1:
        parser.error("--short-stability must be positive")

    api_key = load_api_key()
    original = active_mode()
    stem = "llm-benchmark-" + dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    report: dict[str, Any] = {
        "schema_version": 1,
        "started_at": now_iso(),
        "original_mode": original,
        "host": run(["hostname"]).strip(),
        "models": [],
        "errors": [],
    }

    try:
        for key in keys:
            model = MODELS[key]
            print(f"[{now_iso()}] BEGIN {model.model_id}", flush=True)
            try:
                result = benchmark_model(model, api_key, args.short_stability, args.skip_context)
                report["models"].append(result)
                print(f"[{now_iso()}] COMPLETE {model.model_id}", flush=True)
            except Exception as exc:
                report["errors"].append({"model": model.model_id, "error": repr(exc), "at": now_iso()})
                print(f"[{now_iso()}] ERROR {model.model_id}: {exc!r}", flush=True)
            report["completed_at"] = now_iso()
            report["final_mode"] = active_mode()
            save_report(report, stem)
    finally:
        if not args.no_restore and original in {"copaw", "spark-x1", "qwen38", "qwen-vl", "glm-ocr", "bge-m3", "reranker"} and active_mode() != original:
            try:
                switch_mode(original)
            except Exception as exc:
                report["errors"].append({"model": "mode_restore", "error": repr(exc), "at": now_iso()})
        report["completed_at"] = now_iso()
        report["final_mode"] = active_mode()
        json_path, md_path = save_report(report, stem)
        print(f"[{now_iso()}] JSON={json_path}", flush=True)
        print(f"[{now_iso()}] MARKDOWN={md_path}", flush=True)

    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
