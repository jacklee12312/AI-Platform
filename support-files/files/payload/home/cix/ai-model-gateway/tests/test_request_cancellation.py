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
from app.usage import APIKeyUsageStore


@pytest.fixture
def configured(tmp_path, monkeypatch):
    store = APIKeyUsageStore(tmp_path / "usage.db")
    store.initialize()
    key = store.create_key("cancel-test")
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
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    for name, value in {
        "USAGE_STORE": store,
        "REGISTRY": Registry(registry_path),
        "ADMISSION": AdmissionController(),
        "MANAGEMENT_TOKEN": "unit-admin",
        "GUARD_MODE": "off",
        "LIMITER": gateway.FixedWindowLimiter(1000),
    }.items():
        monkeypatch.setattr(gateway, name, value)
    store.set_resource_policy({"model_limits": {"chat": 4}})
    return (
        store,
        {"Authorization": "Bearer " + key["key"]},
        {"X-Gateway-Admin-Token": "unit-admin"},
    )


def test_cancel_nonstream_waits_for_upstream_cleanup_and_is_idempotent(configured, monkeypatch):
    store, business, management = configured

    async def scenario():
        started = asyncio.Event()
        closing = asyncio.Event()
        cleanup = asyncio.Event()

        async def upstream(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closing.set()
                await cleanup.wait()

        monkeypatch.setattr(gateway, "request_json_async", upstream)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway.app), base_url="http://test"
        ) as client:
            task = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    headers={**business, "X-Request-ID": "caller-reused-id"},
                    json={
                        "model": "chat",
                        "messages": [{"role": "user", "content": "cancel me"}],
                    },
                )
            )
            await asyncio.wait_for(started.wait(), 3)
            active = (
                await client.get("/internal/requests/active", headers=management)
            ).json()
            event_id = active["requests"][0]["id"]
            url = f"/internal/requests/{event_id}/cancel"
            assert (await client.post(url, headers=business)).status_code == 401
            first = await client.post(url, headers=management)
            assert first.status_code == 202
            await asyncio.wait_for(closing.wait(), 3)
            assert gateway.ADMISSION.snapshot()["global"] == 1
            repeated = await client.post(url, headers=management)
            assert repeated.json()["already_requested"] is True
            assert gateway.ADMISSION.request_detail(event_id)["phase"] == "cancelling"
            cleanup.set()
            response = await asyncio.wait_for(task, 3)
            assert response.status_code == 499
            assert response.json()["error"]["code"] == "admin_cancelled"
            assert gateway.ADMISSION.snapshot()["global"] == 0
            history = store.request_detail(event_id)
            assert history["status"] == "cancelled"
            assert history["diagnostics"]["code"] == "admin_cancelled"
            assert (await client.post(url, headers=management)).json()["status"] == "already_finished"

    asyncio.run(scenario())


def test_unsupported_request_cannot_clear_its_slot(configured):
    _, _, management = configured
    lease = gateway.ADMISSION.acquire("key", "embed", [], {}, {})
    try:
        async def scenario():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway.app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/internal/requests/{lease.id}/cancel", headers=management
                )
                assert response.status_code == 409
                assert gateway.ADMISSION.snapshot()["global"] == 1
        asyncio.run(scenario())
    finally:
        lease.finish()


def test_stream_cancel_closes_upstream_and_records_admin_reason(configured, monkeypatch):
    store, business, management = configured
    real_client = httpx.AsyncClient

    async def scenario():
        entered = asyncio.Event()
        closed = asyncio.Event()

        class Stream:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                closed.set()

            async def aclose(self):
                closed.set()

            async def aiter_lines(self):
                yield 'data: {"choices":[{"delta":{"content":"partial"}}]}'
                entered.set()
                await asyncio.Event().wait()

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def stream(self, *args, **kwargs):
                return Stream()

        monkeypatch.setattr(gateway.httpx, "AsyncClient", lambda **kwargs: Client())
        async with real_client(
            transport=httpx.ASGITransport(app=gateway.app), base_url="http://test"
        ) as client:
            task = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    headers=business,
                    json={
                        "model": "chat",
                        "messages": [{"role": "user", "content": "test"}],
                        "stream": True,
                    },
                )
            )
            await asyncio.wait_for(entered.wait(), 3)
            row = (
                await client.get("/internal/requests/active", headers=management)
            ).json()["requests"][0]
            response = await client.post(
                f"/internal/requests/{row['id']}/cancel", headers=management
            )
            assert response.status_code == 202
            streamed = await asyncio.wait_for(task, 3)
            assert closed.is_set()
            assert "admin_cancelled" in streamed.text
            assert "[DONE]" in streamed.text
            assert gateway.ADMISSION.snapshot()["global"] == 0
            rows = store.usage_report()["recent"]
            assert len(rows) == 1
            assert rows[0]["status"] == "cancelled"
            assert rows[0]["diagnostics"]["code"] == "admin_cancelled"

    asyncio.run(scenario())
