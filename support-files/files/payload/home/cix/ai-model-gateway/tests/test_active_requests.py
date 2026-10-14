import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

os.environ.setdefault(
    "MODEL_REGISTRY", str(Path(__file__).resolve().parents[1] / "config/models.json")
)

from app import main as gateway
from app.access import AdmissionController
from app.config import Registry
from app.request_content import sanitize_request_content
from app.request_diagnostics import new_diagnostics
from app.usage import APIKeyUsageStore


@pytest.fixture
def configured(tmp_path, monkeypatch):
    store = APIKeyUsageStore(tmp_path / "usage.db")
    store.initialize()
    key = store.create_key("知识库任务", policy={"application": "文档问答"})
    registry_path = tmp_path / "models.json"
    registry_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "chat",
                        "capability": "chat",
                        "base_url": "http://fake",
                        "backend": "openai",
                    },
                    {
                        "id": "embed",
                        "capability": "embeddings",
                        "base_url": "http://fake-embed",
                        "backend": "openai",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    for name, value in {
        "USAGE_STORE": store,
        "REGISTRY": Registry(registry_path),
        "ADMISSION": AdmissionController(),
        "MANAGEMENT_TOKEN": "unit-admin-secret",
        "GUARD_MODE": "off",
        "LIMITER": gateway.FixedWindowLimiter(1000),
    }.items():
        monkeypatch.setattr(gateway, name, value)
    store.set_resource_policy({"model_limits": {"chat": 4, "embed": 2}})
    return (
        store,
        {"Authorization": "Bearer " + key["key"]},
        {"X-Gateway-Admin-Token": "unit-admin-secret"},
    )


def test_live_registry_uses_unique_internal_ids_and_truthful_finishing_state():
    admission = AdmissionController()
    state = {
        "request_id": "same-client-id",
        "diagnostics": new_diagnostics(),
        "request_content": sanitize_request_content({"input": "任务内容"}),
    }
    first = admission.acquire(
        "key",
        "chat",
        [],
        {},
        {"model_limits": {"chat": 4}},
        metadata={"id": "a" * 32, "state": state, "key_name": "知识库任务"},
    )
    second = admission.acquire(
        "key",
        "chat",
        [],
        {},
        {},
        metadata={"id": "b" * 32, "state": dict(state)},
    )
    snapshot = admission.request_snapshot()
    assert snapshot["total"] == 2
    assert {item["id"] for item in snapshot["requests"]} == {"a" * 32, "b" * 32}
    first.start_work()
    first.finish()
    assert admission.request_detail(first.id)["phase"] == "finishing"
    assert admission.snapshot()["global"] == 2
    first.end_work()
    second.finish()
    assert admission.request_snapshot()["total"] == 0


def test_nonstream_request_moves_from_active_snapshot_to_lazy_history(configured, monkeypatch):
    store, business, management = configured

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def upstream(*args, **kwargs):
            started.set()
            await release.wait()
            return {
                "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
            }

        monkeypatch.setattr(gateway, "request_json_async", upstream)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway.app), base_url="http://test"
        ) as client:
            pending = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    headers={**business, "User-Agent": "test-worker"},
                    json={
                        "model": "chat",
                        "messages": [{"role": "user", "content": "解释文档"}],
                        "stream": False,
                        "max_tokens": 32,
                    },
                )
            )
            await asyncio.wait_for(started.wait(), 3)
            active = (await client.get("/internal/requests/active", headers=management)).json()
            assert active["total"] == active["in_flight"]["global"] == 1
            row = active["requests"][0]
            event_id = row["id"]
            assert row["request_summary"] == "解释文档"
            assert row["request_metadata"]["application"] == "文档问答"
            assert row["request_metadata"]["stream"] is False
            assert row["diagnostics"]["stage"] == "upstream"
            detail = (
                await client.get(f"/internal/requests/{event_id}", headers=management)
            ).json()
            assert detail["request_content"]["payload"]["max_tokens"] == 32
            release.set()
            assert (await pending).status_code == 200
            assert (
                await client.get("/internal/requests/active", headers=management)
            ).json()["total"] == 0
            history = (
                await client.get(f"/internal/requests/{event_id}", headers=management)
            ).json()
            assert history["status"] == "succeeded"
            assert history["diagnostics"]["finish_reason"] == "stop"
            assert history["request_metadata"]["user_agent"] == "test-worker"
            summary = store.usage_report()["recent"][0]
            assert "request_content" not in summary
            assert summary["request_summary"] == "解释文档"

    asyncio.run(scenario())


def test_management_endpoints_do_not_accept_business_key(configured):
    _, business, management = configured
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway.app), base_url="http://test"
        ) as client:
            for path in ("/internal/requests/active", "/internal/requests/" + "a" * 32):
                assert (await client.get(path)).status_code == 401
                assert (await client.get(path, headers=business)).status_code == 401
            assert (
                await client.get("/internal/requests/active", headers=management)
            ).headers["cache-control"] == "no-store"
    asyncio.run(scenario())
