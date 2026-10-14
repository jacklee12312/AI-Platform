import asyncio
import csv
import io
import json
import os
from pathlib import Path
from unittest import mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.access import AdmissionController
from app.config import Registry
from app.request_diagnostics import new_diagnostics
from app.usage import APIKeyUsageStore

with mock.patch.dict(
    os.environ,
    {"MODEL_REGISTRY": str(Path(__file__).resolve().parents[1] / "config/models.json")},
):
    from app import main as gateway


@pytest.fixture
def ledger(tmp_path):
    store = APIKeyUsageStore(tmp_path / "usage.db")
    store.initialize()
    key = store.create_key("=unsafe spreadsheet formula")
    return store, key


def record(store, key, request_id="r", **kwargs):
    store.record_usage(
        api_key_id=key["id"],
        request_id=request_id,
        endpoint="/v1/chat/completions",
        model="chat",
        usage=None,
        status="rejected",
        **kwargs,
    )


def test_legacy_backfill_is_unique_durable_and_does_not_invent_snapshot(ledger):
    store, key = ledger
    for request_id in ["unique", "duplicate"]:
        store.record_rejection(key["id"], request_id, "chat", "model_concurrency_exceeded")
        record(store, key, request_id)
    record(store, key, "duplicate")

    reopened = APIKeyUsageStore(store.path)
    reopened.initialize()
    reopened.initialize()
    row = reopened.usage_report(error_code="model_concurrency_exceeded")["recent"][0]
    assert row["request_id"] == "unique"
    assert row["diagnostics"]["not_inferred"]
    assert row["diagnostics"]["http_status"] == 429
    assert "limit" not in row["diagnostics"]
    assert "elapsed_ms" not in row["diagnostics"]
    assert reopened.usage_report(error_code="historical_unknown")["summary"]["request_count"] == 2
    with reopened._db() as db:
        db.execute("DELETE FROM access_rejections")
    assert reopened.usage_report(error_code="model_concurrency_exceeded")["summary"]["request_count"] == 1


def test_backfill_does_not_cross_key_model_or_old_reused_id(ledger):
    store, key = ledger
    other = store.create_key("other")
    store.record_rejection(other["id"], "same", "chat", "model_concurrency_exceeded")
    store.record_rejection(key["id"], "same", "other-model", "model_concurrency_exceeded")
    store.record_rejection(key["id"], "same", "chat", "model_concurrency_exceeded")
    with store._db() as db:
        db.execute(
            "UPDATE access_rejections SET created_at='2020-01-01T00:00:00Z' "
            "WHERE api_key_id=? AND model='chat'",
            (key["id"],),
        )
    record(store, key, "same")
    reopened = APIKeyUsageStore(store.path)
    reopened.initialize()
    assert reopened.usage_report()["recent"][0]["diagnostics"]["code"] == "historical_unknown"


def test_export_all_filters_unknown_tokens_and_csv_formula_safety(ledger):
    store, key = ledger
    for index in range(505):
        record(
            store,
            key,
            f"r-{index}",
            diagnostics={
                "code": "model_concurrency_exceeded",
                "http_status": 429,
                "upstream_dispatched": False,
                "scope": "model",
                "limit": 4,
                "in_flight": 4,
            },
        )
    record(store, key, "different", diagnostics={"code": "invalid_json"})
    exported = store.export_usage_csv(error_code="model_concurrency_exceeded")
    rows = list(csv.DictReader(io.StringIO(exported["csv"].lstrip("\ufeff"))))
    assert len(rows) == exported["record_count"] == 505
    assert len({row["Request ID"] for row in rows}) == 505
    assert rows[0]["Key名称"].startswith("'=")
    assert rows[0]["输入Token"] == ""
    assert rows[0]["并发快照"] == "4"
    assert key["key"] not in exported["csv"]
    with pytest.raises(ValueError):
        store.export_usage_csv(start="bad")


@pytest.fixture
def client(ledger, tmp_path, monkeypatch):
    store, key = ledger
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "chat",
                        "capability": "chat",
                        "base_url": "http://fake",
                        "backend": "openai",
                    }
                ]
            }
        )
    )
    for name, value in {
        "USAGE_STORE": store,
        "REGISTRY": Registry(path),
        "GUARD_MODE": "off",
        "MANAGEMENT_TOKEN": "unit-management",
        "LIMITER": gateway.FixedWindowLimiter(1000),
        "ADMISSION": AdmissionController(),
    }.items():
        monkeypatch.setattr(gateway, name, value)
    return TestClient(gateway.app), {"Authorization": "Bearer " + key["key"]}


def test_live_rejection_keeps_limit_and_does_not_contact_model(client, ledger):
    client, headers = client
    store, _ = ledger
    store.set_resource_policy({"model_limits": {"chat": 4}})
    leases = [gateway.ADMISSION.acquire("k", "chat", [], {}, {}) for _ in range(4)]
    try:
        with mock.patch.object(gateway, "_call_json") as upstream:
            response = client.post(
                "/v1/chat/completions",
                headers=headers,
                json={"model": "chat", "messages": [{"role": "user", "content": "private"}]},
            )
        assert response.status_code == 429
        upstream.assert_not_called()
        row = store.usage_report(error_code="model_concurrency_exceeded")["recent"][0]
        details = row["diagnostics"]
        assert (
            details["http_status"],
            details["in_flight"],
            details["limit"],
            details["scope"],
        ) == (429, 4, 4, "model")
        assert details["not_inferred"]
        assert details["elapsed_ms"] >= 0
        assert "private" not in json.dumps(row)
    finally:
        for lease in leases:
            lease.finish()


def test_accepted_request_keeps_atomic_model_concurrency_snapshot(client, ledger):
    client, headers = client
    store, _ = ledger
    store.set_resource_policy({"model_limits": {"chat": 4}})
    response_value = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }
    with mock.patch.object(gateway, "_call_json", new=mock.AsyncMock(return_value=response_value)):
        response = client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "chat", "messages": [{"role": "user", "content": "hello"}]},
        )
    assert response.status_code == 200
    details = store.usage_report()["recent"][0]["diagnostics"]
    assert (details["scope"], details["in_flight"], details["limit"]) == ("model", 1, 4)
    assert details["http_status"] == 200
    assert details["elapsed_ms"] >= 0


@pytest.mark.parametrize(
    "case,code,status",
    [
        ("context", "context_length_exceeded", "upstream_error"),
        ("cancel", "client_cancelled", "cancelled"),
        ("timeout", "upstream_timeout", "stream_error"),
        ("success", "", "succeeded"),
    ],
)
def test_stream_diagnostics_distinguish_http_headers_and_final_outcome(
    client, ledger, case, code, status
):
    store, key = ledger

    class Stream:
        status_code = 400 if case == "context" else 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def aread(self):
            return b"maximum context length exceeded secret-key prompt-data"

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"role":"assistant"}}]}'
            yield 'data: {"choices":[{"delta":{"reasoning_content":"private"}}]}'
            if case == "cancel":
                raise asyncio.CancelledError()
            if case == "timeout":
                raise httpx.ReadTimeout("secret-url")
            yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
            yield "data: [DONE]"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def stream(self, *args, **kwargs):
            return Stream()

    async def consume():
        with mock.patch.object(gateway.httpx, "AsyncClient", return_value=Client()):
            try:
                return [
                    chunk
                    async for chunk in gateway._guarded_chat_events(
                        entry=gateway.REGISTRY.get("chat"),
                        model_id="chat",
                        upstream_payload={},
                        guard_ws=None,
                        request_id=case,
                        api_key_id=key["id"],
                        diagnostics=new_diagnostics(),
                    )
                ]
            except asyncio.CancelledError:
                return []

    asyncio.run(consume())
    row = store.usage_report()["recent"][0]
    details = row["diagnostics"]
    assert row["status"] == status
    assert details["code"] == code
    assert details["http_status"] == 200
    assert not details["not_inferred"]
    assert "secret" not in json.dumps(details)
    assert "private" not in json.dumps(details)
    assert details["elapsed_ms"] >= 0
    if case == "context":
        assert details["upstream_http_status"] == 400
    else:
        assert details["output_chunks"] == 1
        assert details["first_output_ms"] >= 0
        assert details["last_output_ms"] == details["first_output_ms"]


def test_export_auth_and_filters(client):
    client, _ = client
    assert client.get("/internal/usage/export").status_code == 401
    admin = {"X-Gateway-Admin-Token": "unit-management"}
    assert client.get("/internal/usage/export?start=bad", headers=admin).status_code == 400
    assert client.get("/internal/usage/export", headers=admin).json()["record_count"] == 0
