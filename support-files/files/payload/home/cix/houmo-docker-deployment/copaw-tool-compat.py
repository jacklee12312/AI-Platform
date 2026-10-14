#!/usr/bin/env python3
"""Read-only CoPaw OpenAI tool-calling compatibility checks."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any


MODEL = "copaw-flash-9b"


def function_tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


WEATHER_TOOL = function_tool(
    "get_weather",
    "查询指定城市的当前天气。",
    {"city": {"type": "string", "description": "城市名称"}},
    ["city"],
)

ARITHMETIC_TOOLS = [
    function_tool(
        "add",
        "计算两个数字的和。",
        {"a": {"type": "number"}, "b": {"type": "number"}},
        ["a", "b"],
    ),
    function_tool(
        "multiply",
        "计算两个数字的乘积。",
        {"a": {"type": "number"}, "b": {"type": "number"}},
        ["a", "b"],
    ),
]

UNSTABLE_TOOL = function_tool(
    "unstable_lookup",
    "查询订单状态；若工具返回临时错误，应重试一次。",
    {"order_id": {"type": "string"}},
    ["order_id"],
)


def post_json(base_url: str, api_key: str, payload: dict[str, Any], timeout: int = 180) -> dict[str, Any]:
    started = time.perf_counter()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    elapsed = round(time.perf_counter() - started, 3)
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {"raw": raw[:2000]}
    return {"status": status, "elapsed_s": elapsed, "body": body}


def message_of(result: dict[str, Any]) -> dict[str, Any]:
    try:
        message = result["body"]["choices"][0]["message"]
        return message if isinstance(message, dict) else {}
    except (KeyError, IndexError, TypeError):
        return {}


def compact_message(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    return {
        "role": message.get("role"),
        "content": content[:1000] if isinstance(content, str) else content,
        "tool_calls": message.get("tool_calls"),
        "function_call": message.get("function_call"),
    }


def tool_calls_of(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("tool_calls")
    return calls if isinstance(calls, list) else []


def validate_calls(calls: list[dict[str, Any]], allowed: set[str]) -> dict[str, Any]:
    errors: list[str] = []
    parsed: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            errors.append(f"call[{index}] missing function object")
            continue
        name = function.get("name")
        if name not in allowed:
            errors.append(f"call[{index}] unknown function: {name!r}")
        arguments = function.get("arguments")
        try:
            decoded = json.loads(arguments) if isinstance(arguments, str) else arguments
            if not isinstance(decoded, dict):
                raise ValueError("arguments are not an object")
            parsed.append({"name": name, "arguments": decoded})
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            errors.append(f"call[{index}] invalid arguments JSON: {exc}")
    return {"valid": bool(calls) and not errors, "errors": errors, "parsed": parsed}


def chat_payload(messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: Any = "auto") -> dict[str, Any]:
    return {
        "model": MODEL,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "temperature": 0,
        "max_tokens": 256,
    }


def assistant_for_history(message: dict[str, Any]) -> dict[str, Any]:
    calls = message.get("tool_calls")
    # Replay the backend response verbatim.  Gateway 0.2.1 must normalize the
    # upstream copy; the client should not need an HLIELlama-specific workaround.
    item: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
    if calls is not None:
        item["tool_calls"] = calls
    if message.get("function_call") is not None:
        item["function_call"] = message["function_call"]
    return item


def run_suite(base_url: str, api_key: str) -> dict[str, Any]:
    report: dict[str, Any] = {"base_url": base_url, "model": MODEL, "cases": {}}

    auto_messages = [
        {"role": "system", "content": "需要外部数据时必须调用提供的工具，不要自行编造工具结果。"},
        {"role": "user", "content": "请调用 get_weather 查询北京当前天气，不要直接回答。"},
    ]
    auto_result = post_json(base_url, api_key, chat_payload(auto_messages, [WEATHER_TOOL]))
    auto_message = message_of(auto_result)
    auto_validation = validate_calls(tool_calls_of(auto_message), {"get_weather"})
    report["cases"]["tools_auto"] = {
        "passed": auto_result["status"] == 200 and auto_validation["valid"],
        "http_status": auto_result["status"],
        "elapsed_s": auto_result["elapsed_s"],
        "validation": auto_validation,
        "message": compact_message(auto_message),
        "error_body": None if auto_result["status"] == 200 else auto_result["body"],
    }

    forced_choice = {"type": "function", "function": {"name": "get_weather"}}
    forced_result = post_json(base_url, api_key, chat_payload(auto_messages, [WEATHER_TOOL], forced_choice))
    forced_message = message_of(forced_result)
    forced_validation = validate_calls(tool_calls_of(forced_message), {"get_weather"})
    report["cases"]["tool_choice_forced"] = {
        "passed": forced_result["status"] == 200 and forced_validation["valid"],
        "http_status": forced_result["status"],
        "elapsed_s": forced_result["elapsed_s"],
        "validation": forced_validation,
        "message": compact_message(forced_message),
        "error_body": None if forced_result["status"] == 200 else forced_result["body"],
    }

    none_messages = [
        {"role": "system", "content": "严格遵守 tool_choice；本轮不要调用工具。"},
        {"role": "user", "content": "只回答 OK。"},
    ]
    none_result = post_json(base_url, api_key, chat_payload(none_messages, [WEATHER_TOOL], "none"))
    none_message = message_of(none_result)
    none_content = none_message.get("content")
    report["cases"]["tool_choice_none"] = {
        "passed": none_result["status"] == 200 and not tool_calls_of(none_message) and isinstance(none_content, str) and "OK" in none_content.upper(),
        "http_status": none_result["status"],
        "elapsed_s": none_result["elapsed_s"],
        "message": compact_message(none_message),
        "error_body": None if none_result["status"] == 200 else none_result["body"],
    }

    required_result = post_json(base_url, api_key, chat_payload(auto_messages, [WEATHER_TOOL], "required"))
    required_message = message_of(required_result)
    required_validation = validate_calls(tool_calls_of(required_message), {"get_weather"})
    report["cases"]["tool_choice_required"] = {
        "passed": required_result["status"] == 200 and required_validation["valid"],
        "http_status": required_result["status"],
        "elapsed_s": required_result["elapsed_s"],
        "validation": required_validation,
        "message": compact_message(required_message),
        "error_body": None if required_result["status"] == 200 else required_result["body"],
    }

    selection_messages = [
        {"role": "system", "content": "从提供的工具中选择与任务匹配的工具，不要调用无关工具。"},
        {"role": "user", "content": "请查询上海当前天气。"},
    ]
    selection_result = post_json(base_url, api_key, chat_payload(selection_messages, [WEATHER_TOOL, *ARITHMETIC_TOOLS]))
    selection_message = message_of(selection_result)
    selection_validation = validate_calls(tool_calls_of(selection_message), {"get_weather", "add", "multiply"})
    selected_names = [row.get("name") for row in selection_validation["parsed"]]
    report["cases"]["multi_tool_selection"] = {
        "passed": selection_result["status"] == 200 and selection_validation["valid"] and selected_names == ["get_weather"],
        "http_status": selection_result["status"],
        "elapsed_s": selection_result["elapsed_s"],
        "selected_names": selected_names,
        "validation": selection_validation,
        "message": compact_message(selection_message),
        "error_body": None if selection_result["status"] == 200 else selection_result["body"],
    }

    first_message = forced_message if forced_validation["valid"] else auto_message
    first_calls = tool_calls_of(first_message)
    loop_messages = list(auto_messages)
    if first_calls:
        loop_messages.append(assistant_for_history(first_message))
        for call in first_calls:
            loop_messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", "call_weather"),
                "name": call.get("function", {}).get("name", "get_weather"),
                "content": json.dumps({"city": "北京", "weather": "晴", "temperature_c": 25}, ensure_ascii=False),
            })
        loop_result = post_json(base_url, api_key, chat_payload(loop_messages, [WEATHER_TOOL]))
        loop_message = message_of(loop_result)
        loop_content = loop_message.get("content")
        loop_passed = (
            loop_result["status"] == 200
            and isinstance(loop_content, str)
            and ("晴" in loop_content or "25" in loop_content)
        )
    else:
        loop_result = {"status": None, "elapsed_s": 0, "body": {"skipped": "no structured tool call"}}
        loop_message = {}
        loop_passed = False
    report["cases"]["tool_result_round_trip"] = {
        "passed": loop_passed,
        "http_status": loop_result["status"],
        "elapsed_s": loop_result["elapsed_s"],
        "message": compact_message(loop_message),
        "error_body": None if loop_result["status"] in (None, 200) else loop_result["body"],
    }

    multi_messages: list[dict[str, Any]] = [
        {"role": "system", "content": "所有算术都必须通过工具完成。请先调用 add，再根据结果调用 multiply，最后给出答案。"},
        {"role": "user", "content": "计算 (7+5)*3。"},
    ]
    multi_trace: list[dict[str, Any]] = []
    invoked: list[str] = []
    final_text = ""
    multi_http_ok = True
    for _ in range(4):
        result = post_json(base_url, api_key, chat_payload(multi_messages, ARITHMETIC_TOOLS))
        message = message_of(result)
        calls = tool_calls_of(message)
        validation = validate_calls(calls, {"add", "multiply"}) if calls else {"valid": False, "errors": [], "parsed": []}
        multi_trace.append({
            "http_status": result["status"],
            "elapsed_s": result["elapsed_s"],
            "validation": validation,
            "message": compact_message(message),
        })
        if result["status"] != 200:
            multi_http_ok = False
            break
        if not calls:
            final_text = message.get("content") if isinstance(message.get("content"), str) else ""
            break
        multi_messages.append(assistant_for_history(message))
        for call in calls:
            function = call.get("function", {})
            name = function.get("name")
            try:
                arguments = json.loads(function.get("arguments", "{}"))
            except json.JSONDecodeError:
                arguments = {}
            if name == "add":
                value: Any = arguments.get("a", 0) + arguments.get("b", 0)
            elif name == "multiply":
                value = arguments.get("a", 0) * arguments.get("b", 0)
            else:
                value = {"error": "unknown tool"}
            invoked.append(str(name))
            multi_messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", f"call_{len(invoked)}"),
                "name": name,
                "content": json.dumps({"result": value}, ensure_ascii=False),
            })
    report["cases"]["multi_step_tool_loop"] = {
        "passed": multi_http_ok and "add" in invoked and "multiply" in invoked and "36" in final_text,
        "invoked": invoked,
        "final_text": final_text[:1000],
        "trace": multi_trace,
    }

    error_messages: list[dict[str, Any]] = [
        {"role": "system", "content": "必须使用工具查询。若工具返回临时错误，请重试一次同一工具。"},
        {"role": "user", "content": "查询订单 A100 的状态。"},
    ]
    error_first = post_json(base_url, api_key, chat_payload(error_messages, [UNSTABLE_TOOL]))
    error_first_message = message_of(error_first)
    error_first_calls = tool_calls_of(error_first_message)
    if error_first_calls:
        error_messages.append(assistant_for_history(error_first_message))
        for call in error_first_calls:
            error_messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", "call_unstable"),
                "name": "unstable_lookup",
                "content": json.dumps({"error": "temporary timeout", "retryable": True}),
            })
        error_second = post_json(base_url, api_key, chat_payload(error_messages, [UNSTABLE_TOOL]))
        error_second_message = message_of(error_second)
        retry_calls = tool_calls_of(error_second_message)
        if retry_calls:
            error_messages.append(assistant_for_history(error_second_message))
            for call in retry_calls:
                error_messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", "call_unstable_retry"),
                    "name": "unstable_lookup",
                    "content": json.dumps({"order_id": "A100", "status": "已发货"}, ensure_ascii=False),
                })
            error_third = post_json(base_url, api_key, chat_payload(error_messages, [UNSTABLE_TOOL]))
            error_third_message = message_of(error_third)
        else:
            error_third = {"status": None, "elapsed_s": 0, "body": {"skipped": "no retry tool call"}}
            error_third_message = {}
    else:
        error_second = {"status": None, "elapsed_s": 0, "body": {"skipped": "no first tool call"}}
        error_second_message = {}
        retry_calls = []
        error_third = {"status": None, "elapsed_s": 0, "body": {"skipped": "no first tool call"}}
        error_third_message = {}
    error_final_content = error_third_message.get("content")
    report["cases"]["tool_error_retry"] = {
        "passed": (
            error_first["status"] == 200
            and error_second["status"] == 200
            and error_third["status"] == 200
            and bool(error_first_calls)
            and any(call.get("function", {}).get("name") == "unstable_lookup" for call in retry_calls)
            and isinstance(error_final_content, str)
            and "已发货" in error_final_content
        ),
        "first_message": compact_message(error_first_message),
        "second_message": compact_message(error_second_message),
        "final_message": compact_message(error_third_message),
        "first_http_status": error_first["status"],
        "second_http_status": error_second["status"],
        "final_http_status": error_third["status"],
        "final_error_body": None if error_third["status"] in (None, 200) else error_third["body"],
    }

    unavailable_prompts = [
        "请调用 get_stock_secret；如果该工具没有提供，请明确说明不可用，不要伪造调用。",
        "请调用 send_email；如果该工具没有提供，请明确说明不可用，不要改用其他工具。",
        "请调用 query_inventory；如果该工具没有提供，请明确说明不可用，不要改用其他工具。",
    ]
    unavailable_runs: list[dict[str, Any]] = []
    for prompt in unavailable_prompts:
        unavailable_messages = [
            {"role": "system", "content": "只能调用请求中提供的工具；不得编造未提供的工具，也不得改用不相关工具。"},
            {"role": "user", "content": prompt},
        ]
        unavailable_result = post_json(base_url, api_key, chat_payload(unavailable_messages, [WEATHER_TOOL]))
        unavailable_message = message_of(unavailable_result)
        unavailable_calls = tool_calls_of(unavailable_message)
        unavailable_content = unavailable_message.get("content")
        run_passed = unavailable_result["status"] == 200 and not unavailable_calls and isinstance(unavailable_content, str)
        unavailable_runs.append({
            "passed": run_passed,
            "http_status": unavailable_result["status"],
            "elapsed_s": unavailable_result["elapsed_s"],
            "message": compact_message(unavailable_message),
            "error_body": None if unavailable_result["status"] == 200 else unavailable_result["body"],
        })
    report["cases"]["unavailable_tool_handling"] = {
        "passed": all(run["passed"] for run in unavailable_runs),
        "runs": unavailable_runs,
    }

    passed = sum(1 for case in report["cases"].values() if case.get("passed"))
    report["summary"] = {
        "passed": passed,
        "total": len(report["cases"]),
        "all_passed": passed == len(report["cases"]),
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:17700")
    parser.add_argument("--output", default="/tmp/copaw-tool-compat.json")
    args = parser.parse_args()
    api_key = os.environ.get("HOUMO_API_KEY")
    if not api_key:
        raise SystemExit("HOUMO_API_KEY is not set")
    report = run_suite(args.base_url, api_key)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps(report["summary"], ensure_ascii=False))
    for name, case in report["cases"].items():
        print(f"{'PASS' if case.get('passed') else 'FAIL'} {name}")
    return 0 if report["summary"]["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
