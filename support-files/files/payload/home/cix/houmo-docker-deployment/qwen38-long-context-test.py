#!/usr/bin/env python3
"""Qwen3.8 four-layer acceptance and selectable Qwen3.8/CoPaw stability tests."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import http.client
import importlib.util
import json
import os
import socket
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


DEPLOY_DIR = Path(os.environ.get("HOUMO_DEPLOY_DIR", "/home/cix/houmo-docker-deployment"))
RESULT_DIR = DEPLOY_DIR / "benchmark-results"
MODEL_ID = "qwen3.8-27b"
CONTAINER = "houmo-qwen38-27b"
MODEL_PORT = 17707
GATEWAY_HOST = "192.168.133.1"
GATEWAY_PORT = 17700
POSITIONS = (0.01, 0.25, 0.50, 0.75, 0.99)
KEYS = ("A", "B", "C", "D", "E")


def load_benchmark_module():
    path = DEPLOY_DIR / "llm-benchmark.py"
    spec = importlib.util.spec_from_file_location("houmo_llm_benchmark", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bench = load_benchmark_module()
MODEL = bench.MODELS["qwen38"]


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def restart_count() -> int:
    try:
        return int(bench.run(["docker", "inspect", "--format", "{{.RestartCount}}", CONTAINER]).strip())
    except Exception:
        return -1


def request_json(
    host: str,
    port: int,
    path: str,
    payload: dict[str, Any],
    api_key: str,
    timeout: float = 1800,
) -> tuple[int, dict[str, Any], float]:
    return bench.http_json(host, port, path, payload, api_key, timeout=timeout)


def tokenize_count(host: str, api_key: str, text: str) -> int:
    status, body, _ = request_json(host, MODEL_PORT, "/tokenize", {"content": text}, api_key, timeout=120)
    if status != 200 or not isinstance(body.get("tokens"), list):
        raise RuntimeError(f"tokenize failed: HTTP {status}: {str(body)[:300]}")
    return len(body["tokens"])


def nonce(seed: str, key: str) -> str:
    value = hashlib.sha256(f"{seed}:needle:{key}".encode()).hexdigest()[:16].upper()
    return f"{key}-{value}"


def ordinary_line(seed: str, index: int) -> str:
    digest = hashlib.sha256(f"{seed}:filler:{index}".encode()).hexdigest()[:24]
    return (
        f"段落{index:06d}：普通记录编号{digest}。该记录仅用于填充上下文，"
        "其中的编号不是答案，也不应出现在最终结果中。\n"
    )


def render_prompt(seed: str, blocks: int) -> tuple[str, dict[str, str]]:
    expected = {key: nonce(seed, key) for key in KEYS}
    marker_indexes = {
        max(0, min(blocks - 1, round(position * (blocks - 1)))): key
        for position, key in zip(POSITIONS, KEYS)
    }
    lines = [
        f"长上下文测试编号：{seed}。",
        "下文包含五条由三尖括号包围的有效记录，其他相似编号全部是干扰项。",
        "完整阅读后，只输出一行严格 JSON，键必须依次为 A、B、C、D、E，值必须逐字复制有效记录中的 value。",
    ]
    for index in range(blocks):
        key = marker_indexes.get(index)
        if key is not None:
            lines.append(f"<<<VALID_NEEDLE key={key} value={expected[key]}>>>\n")
        else:
            lines.append(ordinary_line(seed, index))
    lines.extend([
        "问题：请从全文找出五条 VALID_NEEDLE。",
        '只输出：{"A":"...","B":"...","C":"...","D":"...","E":"..."}',
    ])
    return "\n".join(lines), expected


def build_prompt(host: str, api_key: str, target_tokens: int, seed: str) -> tuple[str, dict[str, str], int]:
    desired = max(512, target_tokens - 48)
    sample, _ = render_prompt(seed, 128)
    sample_tokens = tokenize_count(host, api_key, sample)
    blocks = max(32, int(128 * desired / max(sample_tokens, 1)))
    prompt = sample
    expected: dict[str, str] = {}
    actual = sample_tokens
    for _ in range(4):
        prompt, expected = render_prompt(seed, blocks)
        actual = tokenize_count(host, api_key, prompt)
        error = desired - actual
        if abs(error) <= max(64, int(target_tokens * 0.002)):
            break
        blocks = max(32, int(blocks * desired / max(actual, 1)))
    return prompt, expected, actual


def answer_content(body: dict[str, Any]) -> str:
    try:
        return str(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        return ""


def validate_answer(answer: str, expected: dict[str, str]) -> tuple[bool, dict[str, bool]]:
    # Preserve duplicate keys so a later correct value cannot hide an earlier
    # wrong one. The prompt explicitly requires one strict, ordered JSON object.
    try:
        pairs = json.loads(answer, object_pairs_hook=lambda values: values)
        structure = (isinstance(pairs, list) and len(pairs) == len(KEYS)
                     and all(isinstance(pair, tuple) and len(pair) == 2 for pair in pairs))
        ordered = structure and [pair[0] for pair in pairs] == list(KEYS)
        values = dict(pairs) if structure else {}
    except (ValueError, TypeError):
        values, ordered = {}, False
    per_key = {key: isinstance(values.get(key), str) and values[key] == value
               for key, value in expected.items()}
    return bool(ordered and all(per_key.values())), {**per_key, "ordered": bool(ordered)}


def endpoint(name: str, direct_host: str) -> tuple[str, int]:
    if name == "direct":
        return direct_host, MODEL_PORT
    if name == "gateway":
        return GATEWAY_HOST, GATEWAY_PORT
    raise ValueError(name)


def run_case(
    *,
    endpoint_name: str,
    direct_host: str,
    api_key: str,
    target_tokens: int,
    seed: str,
    sample_resources: bool = True,
) -> dict[str, Any]:
    prompt, expected, tokenizer_tokens = build_prompt(direct_host, api_key, target_tokens, seed)
    payload = {
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 160,
        "stream": False,
    }
    host, port = endpoint(endpoint_name, direct_host)
    before_restart = restart_count()
    sampler = bench.Sampler(CONTAINER)
    try:
        if sample_resources:
            sampler.__enter__()
        status, body, wall = request_json(host, port, "/v1/chat/completions", payload, api_key)
    except Exception as exc:
        return {
            "at": now_iso(), "endpoint": endpoint_name, "target_tokens": target_tokens,
            "tokenizer_tokens": tokenizer_tokens, "seed": seed, "status": 0,
            "correct": False, "error": repr(exc), "restart_before": before_restart,
            "restart_after": restart_count(), "health_after": bench.container_health(CONTAINER),
        }
    finally:
        if sample_resources:
            sampler.__exit__(None, None, None)
    answer = answer_content(body)
    correct, recall = validate_answer(answer, expected)
    usage = body.get("usage", {}) if isinstance(body, dict) else {}
    timings = body.get("timings", {}) if isinstance(body, dict) else {}
    result = {
        "at": now_iso(),
        "endpoint": endpoint_name,
        "target_tokens": target_tokens,
        "tokenizer_tokens": tokenizer_tokens,
        "actual_prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "seed": seed,
        "status": status,
        "wall_seconds": wall,
        "prefill_tokens_per_second": timings.get("prompt_per_second"),
        "decode_tokens_per_second": timings.get("predicted_per_second"),
        "finish_reason": (body.get("choices") or [{}])[0].get("finish_reason") if isinstance(body, dict) else None,
        "correct": status == 200 and correct,
        "recall": recall,
        "answer": answer[:1000],
        "restart_before": before_restart,
        "restart_after": restart_count(),
        "health_after": bench.container_health(CONTAINER),
    }
    if sample_resources:
        result["telemetry"] = sampler.summary()
    if status != 200:
        result["error"] = body
    return result


def stream_case(
    *, direct_host: str, api_key: str, target_tokens: int, seed: str,
) -> dict[str, Any]:
    prompt, expected, tokenizer_tokens = build_prompt(direct_host, api_key, target_tokens, seed)
    payload = {
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 160,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    conn = http.client.HTTPConnection(direct_host, MODEL_PORT, timeout=1800)
    started = time.monotonic()
    first_event = None
    first_content = None
    pieces: list[str] = []
    usage: dict[str, Any] = {}
    status = 0
    error = None
    with bench.Sampler(CONTAINER) as sampler:
        try:
            conn.request("POST", "/v1/chat/completions", body=raw, headers={
                "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
            })
            response = conn.getresponse()
            status = response.status
            while True:
                line = response.readline()
                if not line:
                    break
                if not line.startswith(b"data:"):
                    continue
                elapsed = time.monotonic() - started
                if first_event is None:
                    first_event = elapsed
                data = line[5:].strip()
                if data == b"[DONE]":
                    continue
                event = json.loads(data)
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                for choice in event.get("choices", []):
                    content = (choice.get("delta") or {}).get("content")
                    if isinstance(content, str) and content:
                        if first_content is None:
                            first_content = elapsed
                        pieces.append(content)
        except Exception as exc:
            error = repr(exc)
        finally:
            conn.close()
    answer = "".join(pieces)
    correct, recall = validate_answer(answer, expected)
    return {
        "at": now_iso(), "target_tokens": target_tokens, "tokenizer_tokens": tokenizer_tokens,
        "actual_prompt_tokens": usage.get("prompt_tokens"), "seed": seed, "status": status,
        "response_first_event_seconds": first_event, "ttft_seconds": first_content,
        "wall_seconds": time.monotonic() - started, "correct": status == 200 and correct,
        "recall": recall, "answer": answer[:1000], "error": error,
        "restart_after": restart_count(), "health_after": bench.container_health(CONTAINER),
        "telemetry": sampler.summary(),
    }


def short_recovery(direct_host: str, api_key: str) -> dict[str, Any]:
    payload = {
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": "严格只回答 RECOVERY_OK"}],
        "temperature": 0,
        "max_tokens": 16,
    }
    try:
        status, body, elapsed = request_json(direct_host, MODEL_PORT, "/v1/chat/completions", payload, api_key, 300)
        answer = answer_content(body)
        return {
            "status": status, "elapsed_seconds": elapsed, "answer": answer[:200],
            "passed": status == 200 and "RECOVERY_OK" in answer,
            "health": bench.container_health(CONTAINER), "restart_count": restart_count(),
        }
    except Exception as exc:
        return {"status": 0, "passed": False, "error": repr(exc), "restart_count": restart_count()}


def oversize_case(direct_host: str, api_key: str, target_tokens: int, seed: str) -> dict[str, Any]:
    prompt, _, tokenizer_tokens = build_prompt(direct_host, api_key, target_tokens, seed)
    payload = {
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 256,
    }
    before = restart_count()
    try:
        status, body, elapsed = request_json(direct_host, MODEL_PORT, "/v1/chat/completions", payload, api_key, 600)
        accepted_cleanly = status in {400, 413, 422}
        detail = str(body)[:1000]
    except Exception as exc:
        status, elapsed, accepted_cleanly, detail = 0, None, False, repr(exc)
    recovery = short_recovery(direct_host, api_key)
    return {
        "at": now_iso(), "target_tokens": target_tokens, "tokenizer_tokens": tokenizer_tokens,
        "status": status, "elapsed_seconds": elapsed, "passed": accepted_cleanly and recovery.get("passed"),
        "response": detail, "restart_before": before, "restart_after": restart_count(),
        "recovery": recovery,
    }


def disconnect_case(direct_host: str, api_key: str) -> dict[str, Any]:
    prompt, _, tokenizer_tokens = build_prompt(direct_host, api_key, 120000, "disconnect-120k")
    payload = {
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 256,
        "stream": True,
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    before = restart_count()
    error = None
    try:
        conn = http.client.HTTPConnection(direct_host, MODEL_PORT, timeout=30)
        conn.putrequest("POST", "/v1/chat/completions")
        conn.putheader("Authorization", f"Bearer {api_key}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(raw)))
        conn.endheaders()
        conn.send(raw)
        conn.close()
    except Exception as exc:
        error = repr(exc)
    time.sleep(3)
    recovery = short_recovery(direct_host, api_key)
    return {
        "at": now_iso(), "tokenizer_tokens": tokenizer_tokens, "send_error": error,
        "restart_before": before, "restart_after": restart_count(),
        "passed": recovery.get("passed") and restart_count() == before,
        "recovery": recovery,
    }


def concurrency_case(direct_host: str, api_key: str) -> dict[str, Any]:
    def worker(index: int) -> dict[str, Any]:
        return run_case(
            endpoint_name="direct", direct_host=direct_host, api_key=api_key,
            target_tokens=65536, seed=f"concurrent-{index}", sample_resources=False,
        )

    before = restart_count()
    started = time.monotonic()
    with bench.Sampler(CONTAINER) as sampler:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(worker, (1, 2)))
    return {
        "at": now_iso(), "concurrency": 2, "slot_count": 1,
        "wall_seconds": time.monotonic() - started,
        "passed": all(row.get("status") == 200 and row.get("correct") for row in rows),
        "rows": rows, "restart_before": before, "restart_after": restart_count(),
        "health_after": bench.container_health(CONTAINER), "telemetry": sampler.summary(),
    }


def summarize_rows(rows: list[dict[str, Any]]) -> tuple[int, int]:
    return sum(bool(row.get("correct")) for row in rows), len(rows)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# {report.get('model', MODEL_ID)} 长上下文测试", "",
        f"- 开始：{report.get('started_at')}",
        f"- 更新：{report.get('updated_at')}",
        f"- 阶段：{report.get('phase')}",
        f"- 运行模式：{report.get('active_mode')}",
        f"- 实际槽位上下文：{report.get('context_size', 131072)} tokens，槽位数：{report.get('slots', 1)}", "",
    ]
    for title, key in (
        ("第一层：容量边界", "capacity"),
        ("第二层：多针召回", "accuracy"),
        ("第三层：性能和产品路径", "performance"),
    ):
        rows = report.get(key, [])
        passed, total = summarize_rows(rows)
        lines.extend([f"## {title}", "", f"- 正确：{passed}/{total}", "",
                      "|目标|实际输入|端点|HTTP|正确|耗时(s)|Prefill(tok/s)|TTFT(s)|", "|---:|---:|---|---:|---|---:|---:|---:|"])
        for row in rows:
            lines.append(
                f"|{row.get('target_tokens')}|{row.get('actual_prompt_tokens', row.get('tokenizer_tokens'))}|"
                f"{row.get('endpoint', 'direct-stream')}|{row.get('status')}|{row.get('correct')}|"
                f"{row.get('wall_seconds')}|{row.get('prefill_tokens_per_second')}|{row.get('ttft_seconds')}|"
            )
        lines.append("")
    anomalies = report.get("anomalies", [])
    lines.extend(["## 第四层：并发、异常与恢复", ""])
    for row in anomalies:
        lines.append(f"- `{row.get('name')}`：{'PASS' if row.get('passed') else 'FAIL'}")
    stability = report.get("stability", {})
    if stability:
        rows = stability.get("rows", [])
        ok = sum(row.get("status") == 200 and row.get("correct") for row in rows)
        lines.extend(["", "## 持续稳定性", "",
                      f"- 计划时长：{stability.get('requested_minutes')} 分钟",
                      f"- 预计结束：{stability.get('expected_finish_at')}（最后一条在途请求可能延后结束）",
                      f"- 完成时间：{stability.get('completed_at', '运行中')}",
                      f"- 上下文目标：{stability.get('targets')}",
                      f"- 请求：{len(rows)}，成功且召回正确：{ok}",
                      f"- 已运行：{stability.get('elapsed_seconds')} 秒",
                      f"- 容器重启：{stability.get('restart_start')} → {stability.get('restart_end')}",
                      f"- 初始/当前容器内存：{stability.get('memory_start_bytes')} / {stability.get('memory_end_bytes')}"])
    return "\n".join(lines) + "\n"


def checkpoint(report: dict[str, Any], stem: str) -> tuple[Path, Path]:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    report["updated_at"] = now_iso()
    report["active_mode"] = bench.active_mode()
    json_path = RESULT_DIR / f"{stem}.json"
    md_path = RESULT_DIR / f"{stem}.md"
    for path, content in (
        (json_path, json.dumps(report, ensure_ascii=False, indent=2)),
        (md_path, render_markdown(report)),
    ):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    return json_path, md_path


def run_quick(args: argparse.Namespace, api_key: str, direct_host: str, stem: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": 1, "phase": "layers-1-to-4-quick", "started_at": now_iso(),
        "model": MODEL_ID, "context_size": args.context_size, "slots": args.slots,
        "restart_start": restart_count(), "baseline": bench.baseline_resources(MODEL),
        "capacity": [], "accuracy": [], "performance": [], "anomalies": [], "errors": [],
    }
    checkpoint(report, stem)

    for target in (8192, 32768, 65536, 98304, 120000, 126500):
        print(f"[{now_iso()}] L1 capacity target={target}", flush=True)
        row = run_case(endpoint_name="direct", direct_host=direct_host, api_key=api_key,
                       target_tokens=target, seed=f"capacity-{target}")
        report["capacity"].append(row)
        checkpoint(report, stem)

    for target in (32768, 65536, 98304, 120000, 126000):
        for seed_index in range(1, 6):
            print(f"[{now_iso()}] L2 recall target={target} seed={seed_index}/5", flush=True)
            row = run_case(endpoint_name="direct", direct_host=direct_host, api_key=api_key,
                           target_tokens=target, seed=f"recall-{target}-{seed_index}")
            report["accuracy"].append(row)
            checkpoint(report, stem)

    for target in (8192, 65536, 120000, 126000):
        print(f"[{now_iso()}] L3 stream performance target={target}", flush=True)
        report["performance"].append(stream_case(
            direct_host=direct_host, api_key=api_key, target_tokens=target,
            seed=f"stream-{target}",
        ))
        checkpoint(report, stem)

    for target in (8192, 65536, 126000):
        print(f"[{now_iso()}] L3 gateway target={target}", flush=True)
        row = run_case(endpoint_name="gateway", direct_host=direct_host, api_key=api_key,
                       target_tokens=target, seed=f"gateway-{target}")
        report["performance"].append(row)
        checkpoint(report, stem)

    print(f"[{now_iso()}] L4 concurrency=2 at 64K", flush=True)
    concurrent_row = concurrency_case(direct_host, api_key)
    concurrent_row["name"] = "two_64k_requests_one_slot"
    report["anomalies"].append(concurrent_row)
    checkpoint(report, stem)

    for target in (131200, 140000):
        print(f"[{now_iso()}] L4 oversize target={target}", flush=True)
        row = oversize_case(direct_host, api_key, target, f"oversize-{target}")
        row["name"] = f"oversize_{target}"
        report["anomalies"].append(row)
        checkpoint(report, stem)

    print(f"[{now_iso()}] L4 client disconnect during 120K request", flush=True)
    row = disconnect_case(direct_host, api_key)
    row["name"] = "disconnect_during_120k"
    report["anomalies"].append(row)
    report["recovery_final"] = short_recovery(direct_host, api_key)
    report["restart_end"] = restart_count()
    report["final_resources"] = bench.baseline_resources(MODEL)
    checkpoint(report, stem)
    return report


def run_stability(
    args: argparse.Namespace, api_key: str, direct_host: str, stem: str,
) -> dict[str, Any]:
    duration_seconds = args.minutes * 60
    report: dict[str, Any] = {
        "schema_version": 1, "phase": "layer-4-stability", "started_at": now_iso(),
        "model": MODEL_ID, "context_size": args.context_size, "slots": args.slots,
        "pid": os.getpid(), "container": CONTAINER,
        "capacity": [], "accuracy": [], "performance": [], "anomalies": [],
        "stability": {
            "requested_minutes": args.minutes, "restart_start": restart_count(),
            "expected_finish_at": (dt.datetime.now().astimezone() + dt.timedelta(minutes=args.minutes)).isoformat(timespec="seconds"),
            "targets": list(args.targets), "concurrency": 1, "stream": False,
            "memory_start_bytes": bench.baseline_resources(MODEL).get("container_memory_bytes"),
            "rows": [],
        },
    }
    checkpoint(report, stem)
    started = time.monotonic()
    targets = args.targets
    index = 0
    consecutive_failures = 0
    while time.monotonic() - started < duration_seconds:
        target = targets[index % len(targets)]
        seed = f"stability-{index:06d}-{int(started)}"
        print(f"[{now_iso()}] stability request={index + 1} target={target}", flush=True)
        try:
            row = run_case(endpoint_name="gateway", direct_host=direct_host, api_key=api_key,
                           target_tokens=target, seed=seed)
        except Exception as exc:
            # A transient tokenizer/telemetry error must be recorded, not silently
            # terminate a four-hour background run before its final report.
            row = {"at": now_iso(), "target_tokens": target, "seed": seed,
                   "status": 0, "correct": False, "error": repr(exc)}
            time.sleep(5)
        report["stability"]["rows"].append(row)
        if row.get("status") == 200 and row.get("correct"):
            consecutive_failures = 0
        else:
            consecutive_failures += 1
        report["stability"]["elapsed_seconds"] = time.monotonic() - started
        report["stability"]["restart_end"] = restart_count()
        report["stability"]["memory_end_bytes"] = bench.baseline_resources(MODEL).get("container_memory_bytes")
        checkpoint(report, stem)
        if consecutive_failures >= 3:
            recovery = short_recovery(direct_host, api_key)
            report["anomalies"].append({"name": f"recovery_after_{index + 1}", "passed": recovery.get("passed"), "recovery": recovery})
            consecutive_failures = 0
            checkpoint(report, stem)
        index += 1
        if args.max_requests and index >= args.max_requests:
            break
    report["stability"]["completed_at"] = now_iso()
    report["stability"]["elapsed_seconds"] = time.monotonic() - started
    report["stability"]["duration_completed"] = time.monotonic() - started >= duration_seconds
    report["stability"]["restart_end"] = restart_count()
    report["stability"]["memory_end_bytes"] = bench.baseline_resources(MODEL).get("container_memory_bytes")
    report["recovery_final"] = short_recovery(direct_host, api_key)
    checkpoint(report, stem)
    return report


def main() -> int:
    global MODEL, MODEL_ID, CONTAINER, MODEL_PORT
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=("qwen38", "copaw"), default="qwen38")
    parser.add_argument("--phase", choices=("quick", "stability"), default="quick")
    parser.add_argument("--targets", type=int, nargs="+", default=(8192, 65536, 118000, 118000))
    parser.add_argument("--minutes", type=float, default=240.0)
    parser.add_argument("--max-requests", type=int, default=0)
    parser.add_argument("--stem")
    parser.add_argument("--restore-mode", choices=("copaw", "spark-x1", "qwen38", "qwen-vl", "glm-ocr", "bge-m3", "reranker"))
    args = parser.parse_args()
    if args.minutes <= 0:
        parser.error("--minutes must be positive")
    if args.max_requests < 0 or any(target < 512 for target in args.targets):
        parser.error("--max-requests must be nonnegative and --targets must be at least 512")
    if args.model != "qwen38" and args.phase != "stability":
        parser.error("CoPaw currently supports --phase stability only")
    if args.stem and (Path(args.stem).name != args.stem or args.stem in {".", ".."}):
        parser.error("--stem must be a filename, not a path")

    MODEL = bench.MODELS[args.model]
    MODEL_ID, CONTAINER, MODEL_PORT = MODEL.model_id, MODEL.container, MODEL.port
    # One stability runner owns the accelerator at a time, including across SSH sessions.
    import fcntl
    bench.STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = (bench.STATE_DIR / "long-context-test.lock").open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("another long-context test is already running")

    original_mode = bench.active_mode()
    bench.switch_mode(MODEL.mode)
    bench.wait_healthy(CONTAINER)
    api_key = bench.load_api_key()
    direct_host = bench.container_address(MODEL)
    with urllib.request.urlopen(urllib.request.Request(
        f"http://{direct_host}:{MODEL_PORT}/props", headers={"Authorization": f"Bearer {api_key}"},
    ), timeout=30) as response:
        props = json.load(response)
    args.slots = int(props["total_slots"])
    args.context_size = int(props["default_generation_settings"]["n_ctx"])
    if args.phase == "stability" and max(args.targets) > args.context_size - 512:
        parser.error(f"targets exceed the actual per-slot context ({args.context_size} minus output reserve)")
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    stem = args.stem or f"{args.model}-long-context-{args.phase}-{stamp}"
    if (RESULT_DIR / f"{stem}.json").exists():
        raise RuntimeError(f"report already exists; refusing to overwrite: {stem}")
    print(f"[{now_iso()}] phase={args.phase} direct_host={direct_host} stem={stem}", flush=True)
    exit_code = 0
    try:
        if args.phase == "quick":
            report = run_quick(args, api_key, direct_host, stem)
            all_rows = report["capacity"] + report["accuracy"] + report["performance"]
            if any(row.get("status") != 200 or not row.get("correct") for row in all_rows):
                exit_code = 2
            if any(not row.get("passed") for row in report["anomalies"]):
                exit_code = 2
        else:
            report = run_stability(args, api_key, direct_host, stem)
            rows = report["stability"]["rows"]
            if any(row.get("status") != 200 or not row.get("correct") for row in rows):
                exit_code = 2
            stability = report["stability"]
            if (not stability["duration_completed"]
                    or stability["restart_end"] != stability["restart_start"]
                    or not report.get("recovery_final", {}).get("passed")):
                exit_code = 2
    finally:
        restore = args.restore_mode
        if restore and bench.active_mode() != restore:
            bench.switch_mode(restore)
        elif not restore and original_mode not in {MODEL.mode, "unknown"}:
            print(f"[{now_iso()}] original mode was {original_mode}; left {MODEL.mode} active for the next phase", flush=True)
    print(f"[{now_iso()}] JSON={RESULT_DIR / (stem + '.json')}", flush=True)
    print(f"[{now_iso()}] MARKDOWN={RESULT_DIR / (stem + '.md')}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
