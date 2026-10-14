"""Policy enforcement and admission lifetime tests; never call real model hardware."""
import asyncio
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.access import (AdmissionController, AdmissionLifetimeMiddleware, KeyPolicy, ResourcePolicy,
                        upstream_thread)
from app.config import Registry
from app.errors import GatewayError
from app.usage import APIKeyUsageStore
with mock.patch.dict(os.environ, {"MODEL_REGISTRY": str(Path(__file__).resolve().parents[1] / "config/models.json")}):
    from app import main as gateway


class PolicyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = APIKeyUsageStore(Path(self.temp.name) / "usage.db")
        self.store.initialize()
        self.key = self.store.create_key("client")

    def test_defaults_expiry_and_explicit_clear_without_rotating_key(self):
        self.assertIsNone(self.key["policy"]["allowed_models"])
        self.store.update_key(self.key["id"], policy={"expires_at": "2020-01-01T08:00:00+08:00", "allowed_models": []})
        self.assertIsNone(self.store.authenticate(self.key["key"]))
        self.assertTrue(self.store.get_key(self.key["id"])["access"]["expired"])
        self.store.update_key(self.key["id"], policy={"expires_at": None, "allowed_models": None})
        self.assertIsNotNone(self.store.authenticate(self.key["key"]))
        self.assertEqual(self.key["prefix"], self.store.get_key(self.key["id"])["prefix"])

    def test_policy_rejects_invalid_limits_dates_and_unknown_fields(self):
        for policy in [{"max_concurrent_requests": -1}, {"max_concurrent_requests": True},
                       {"daily_request_limit": 1.1}, {"expires_at": "2026-12-01"},
                       {"expires_at": "2026-02-30T00:00:00Z"}, {"max_concurrent_requests": 1025},
                       {"allowed_models": [" "]}, {"unrecognized": 1}]:
            with self.subTest(policy=policy), self.assertRaises(ValidationError):
                KeyPolicy.model_validate(policy)
        with self.assertRaises(ValidationError): ResourcePolicy(pool_limits={"typo": 1})

    def test_daily_quota_is_atomic_persistent_and_not_reset_by_edit(self):
        self.store.update_key(self.key["id"], policy={"daily_request_limit": 5})
        def reserve(_):
            try:
                self.store.reserve_key_request(self.key["id"], "chat")
                return True
            except GatewayError as error:
                self.assertEqual(error.code, "daily_quota_exceeded")
                return False
        with ThreadPoolExecutor(max_workers=10) as pool:
            self.assertEqual(sum(pool.map(reserve, range(30))), 5)
        self.store.update_key(self.key["id"], policy={"daily_request_limit": 6})
        other = APIKeyUsageStore(self.store.path); other.initialize()
        other.reserve_key_request(self.key["id"], "chat")
        self.assertEqual(other.get_key(self.key["id"])["access"]["daily_remaining"], 0)
        self.assertEqual(other.usage_report()["summary"]["request_count"], 0)

    def test_quota_day_uses_beijing_midnight(self):
        self.store.update_key(self.key["id"], policy={"daily_request_limit": 1})
        with mock.patch("app.access_store.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 7, 15, 59, 59, tzinfo=timezone.utc)
            self.store.reserve_key_request(self.key["id"], "chat")
            with self.assertRaises(GatewayError): self.store.reserve_key_request(self.key["id"], "chat")
            clock.now.return_value += timedelta(seconds=1)
            self.store.reserve_key_request(self.key["id"], "chat")

    def test_warning_does_not_turn_unknown_tokens_into_zero_or_block(self):
        self.store.update_key(self.key["id"], policy={"monthly_token_warning": 10})
        for usage in [None, {"total_tokens": 12}]:
            self.store.record_usage(api_key_id=self.key["id"], request_id="r", model="chat", endpoint="/v1/chat/completions", usage=usage)
        info = self.store.get_key(self.key["id"])["access"]
        self.assertTrue(info["token_warning_reached"])
        self.assertEqual(info["monthly_known_tokens"], 12)
        self.assertGreaterEqual(info["monthly_uncertain_requests"], 1)
        self.store.reserve_key_request(self.key["id"], "chat")

    def test_revocation_and_allowlist_rechecked_when_reserving(self):
        self.store.update_key(self.key["id"], policy={"allowed_models": []})
        with self.assertRaises(GatewayError) as blocked: self.store.reserve_key_request(self.key["id"], "chat")
        self.assertEqual(blocked.exception.code, "model_not_allowed")
        self.store.revoke_key(self.key["id"])
        with self.assertRaises(GatewayError) as blocked: self.store.reserve_key_request(self.key["id"], "chat")
        self.assertEqual(blocked.exception.status_code, 401)


class GatewayAccessTests(unittest.TestCase):
    def setUp(self):
        PolicyStoreTests.setUp(self)
        document = json.loads((Path(__file__).resolve().parents[1] / "config/models.json").read_text(encoding="utf-8"))
        for model in document["models"]:
            model.update(modes=[], guard=False)
        path = Path(self.temp.name) / "models.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        for name, value in [("REGISTRY", Registry(path)), ("USAGE_STORE", self.store), ("API_KEY", ""),
                            ("MANAGEMENT_TOKEN", "access-test-admin"), ("GUARD_MODE", "off"),
                            ("ADMISSION", AdmissionController()), ("LIMITER", gateway.FixedWindowLimiter(1000))]:
            patch = mock.patch.object(gateway, name, value); patch.start(); self.addCleanup(patch.stop)
        self.client = TestClient(gateway.app)
        self.headers = {"Authorization": "Bearer " + self.key["key"]}
        self.admin = {"X-Gateway-Admin-Token": "access-test-admin"}
        self.chat = {"model": "copaw-flash-9b", "messages": [{"role": "user", "content": "test"}]}

    def test_all_inference_routes_and_aliases_enforce_allowlist(self):
        self.store.update_key(self.key["id"], policy={"allowed_models": ["copaw-flash-9b"]})
        cases = [("/v1/embeddings", {"json": {"model": "bge-m3", "input": "x"}}),
                 ("/v1/rerank", {"json": {"model": "bge-reranker-v2-m3", "query": "x", "documents": ["y"]}}),
                 ("/v1/moderations", {"json": {"input": "x"}}),
                 ("/v1/cv/classify", {"content": b"image"}),
                 ("/v1/audio/speech", {"json": {"model": "matcha", "input": "hello"}}),
                 ("/v1/audio/tts", {"json": {"input": "hello"}}),
                 ("/v1/audio/transcriptions", {"content": b"audio", "headers": {**self.headers, "Content-Type": "multipart/form-data; boundary=x"}}),
                 ("/v1/audio/asr", {"content": b"audio", "headers": {**self.headers, "Content-Type": "multipart/form-data; boundary=x"}}),
                 ("/v1/ocr/mineru", {"json": self.chat})]
        with mock.patch.object(gateway, "_call_json", new=mock.AsyncMock()) as upstream, mock.patch.object(gateway, "request_bytes") as audio:
            for route, kwargs in cases:
                kwargs.setdefault("headers", self.headers)
                with self.subTest(route=route):
                    response = self.client.post(route, **kwargs)
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(response.json()["error"]["code"], "model_not_allowed")
            upstream.assert_not_called(); audio.assert_not_called()
        self.assertEqual(self.store.get_key(self.key["id"])["access"]["daily_requests"], 0)
        self.assertEqual(len(self.store.recent_rejections()), len(cases))

    def test_model_discovery_expiry_rpm_and_clear_policy(self):
        self.store.update_key(self.key["id"], policy={"allowed_models": [self.chat["model"]], "rate_limit_per_minute": 1})
        with mock.patch.object(gateway, "_call_json", new=mock.AsyncMock(return_value={"ok": True})):
            self.assertEqual([x["id"] for x in self.client.get("/v1/models", headers=self.headers).json()["data"]], [self.chat["model"]])
        response = self.client.post("/v1/chat/completions", json=self.chat, headers=self.headers)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "60")
        self.store.update_key(self.key["id"], policy={"expires_at": "2020-01-01T00:00:00Z"})
        self.assertEqual(self.client.get("/v1/models", headers=self.headers).json()["error"]["code"], "api_key_expired")
        response = self.client.patch('/internal/api-keys/' + self.key['id'], headers=self.admin, json={'policy': {'expires_at': None, 'allowed_models': None}})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['policy']['expires_at'])

    def test_resource_admin_auth_validation_and_persistence(self):
        self.assertEqual(self.client.get("/internal/resource-policy").status_code, 401)
        self.assertEqual(self.client.put("/internal/resource-policy", headers=self.admin, json={"model_limits": {"unknown": 1}}).status_code, 400)
        self.assertEqual(self.client.post("/internal/api-keys", headers=self.admin, json={"name": "bad", "policy": {"allowed_models": ["unknown"]}}).status_code, 400)
        expected = {"global_max_concurrent": 5, "model_limits": {self.chat["model"]: 2}, "pool_limits": {"LQ50": 3}}
        self.assertEqual(self.client.put("/internal/resource-policy", headers=self.admin, json=expected).status_code, 200)
        other = APIKeyUsageStore(self.store.path); other.initialize()
        self.assertEqual(other.get_resource_policy(), expected)

    def test_editing_quota_does_not_silently_expand_model_permissions(self):
        self.store.update_key(self.key['id'], policy={'allowed_models': [], 'expires_at': '2030-01-01T00:00:00Z'})
        result = self.client.patch('/internal/api-keys/' + self.key['id'], headers=self.admin,
                                  json={'policy': {'daily_request_limit': 50}})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['policy']['allowed_models'], [])
        self.assertEqual(result.json()['policy']['expires_at'], '2030-01-01T00:00:00Z')

    def test_quota_and_upstream_failure_release_and_management_exclusion(self):
        self.store.update_key(self.key["id"], policy={"daily_request_limit": 1, "max_concurrent_requests": 1})
        with mock.patch.object(gateway, "_call_json", new=mock.AsyncMock(side_effect=GatewayError(503, "fake failure", "upstream_unavailable"))):
            self.assertEqual(self.client.post('/v1/chat/completions', headers=self.headers, json=self.chat).status_code, 503)
        self.assertEqual(gateway.ADMISSION.snapshot()["global"], 0)
        response = self.client.post('/v1/chat/completions', headers=self.headers, json=self.chat)
        self.assertEqual(response.json()['error']['code'], 'daily_quota_exceeded')
        self.assertEqual(gateway.ADMISSION.snapshot()["global"], 0)
        with mock.patch.object(gateway, "_call_json", new=mock.AsyncMock(return_value={"choices": [], "usage": {"total_tokens": 1}})):
            self.assertEqual(self.client.post('/v1/chat/completions', headers=self.admin, json=self.chat).status_code, 200)
        self.assertEqual(self.store.get_key(self.key['id'])['access']['daily_requests'], 1)

    def test_websocket_denial_before_upstream(self):
        self.store.update_key(self.key["id"], policy={"allowed_models": []})
        with mock.patch.object(gateway.websockets, 'connect') as upstream:
            with self.client.websocket_connect('/v1/audio/transcriptions/stream', headers=self.headers) as ws:
                self.assertEqual(ws.receive_json()['code'], 'model_not_allowed')
            upstream.assert_not_called()
        self.assertEqual(gateway.ADMISSION.snapshot()['global'], 0)

    def test_sse_holds_slot_until_done_and_releases_on_cancel(self):
        self.store.set_resource_policy({'model_limits': {self.chat['model']: 1}})
        async def exercise(cancel):
            entered, finish = asyncio.Event(), asyncio.Event()
            async def events(**kwargs):
                entered.set()
                yield 'data: {"choices":[]}\n\n'
                await finish.wait()
                yield 'data: [DONE]\n\n'
            with mock.patch.object(gateway, '_guarded_chat_events', events):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url='http://test') as client:
                    running = asyncio.create_task(client.post('/v1/chat/completions', headers=self.headers, json={**self.chat, 'stream': True}))
                    await asyncio.wait_for(entered.wait(), 3)
                    rejected = await client.post('/v1/chat/completions', headers=self.admin, json=self.chat)
                    self.assertEqual(rejected.json()['error']['code'], 'model_concurrency_exceeded')
                    self.assertEqual(gateway.ADMISSION.snapshot()['global'], 1)
                    if cancel:
                        running.cancel()
                        with self.assertRaises(asyncio.CancelledError): await running
                    else:
                        finish.set()
                        self.assertEqual((await running).status_code, 200)
                    self.assertEqual(gateway.ADMISSION.snapshot()['global'], 0)
        asyncio.run(exercise(False)); asyncio.run(exercise(True))

    def test_websocket_holds_shared_slot_then_releases_on_disconnect(self):
        self.store.update_key(self.key['id'], policy={'max_concurrent_requests': 1})
        class FakeASR:
            def __init__(self): self.closed = asyncio.Event()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): self.closed.set()
            def __aiter__(self): return self.messages()
            async def messages(self):
                yield '{"type":"ready"}'
                await self.closed.wait()
            async def close(self): self.closed.set()
        with mock.patch.object(gateway.websockets, 'connect', side_effect=lambda *a, **k: FakeASR()) as upstream:
            with self.client.websocket_connect('/v1/audio/transcriptions/stream', headers=self.headers) as ws:
                self.assertEqual(ws.receive_json()['type'], 'ready')
                self.assertEqual(gateway.ADMISSION.snapshot()['global'], 1)
                rejected = self.client.post('/v1/chat/completions', json=self.chat, headers=self.headers)
                self.assertEqual(rejected.json()['error']['code'], 'key_concurrency_exceeded')
            self.assertEqual(upstream.call_count, 1)
        self.assertEqual(gateway.ADMISSION.snapshot()['global'], 0)
        self.assertEqual(self.store.get_key(self.key['id'])['access']['daily_requests'], 1)


class AdmissionTests(unittest.TestCase):
    def test_limits_are_shared_across_keys_and_models_and_no_partial_acquire(self):
        for resources, policy, next_key, next_model, code in [
            ({'global_max_concurrent': 1}, {}, 'b', 'n', 'global_concurrency_exceeded'),
            ({}, {'max_concurrent_requests': 1}, 'a', 'n', 'key_concurrency_exceeded'),
            ({'model_limits': {'m': 1}}, {}, 'b', 'm', 'model_concurrency_exceeded'),
            ({'pool_limits': {'LQ50': 1}}, {}, 'b', 'n', 'pool_concurrency_exceeded')]:
            controller = AdmissionController()
            lease = controller.acquire('a', 'm', ['LQ50'], policy, resources)
            with self.assertRaises(GatewayError) as blocked: controller.acquire(next_key, next_model, ['LQ50'], policy, resources)
            self.assertEqual(blocked.exception.code, code)
            self.assertEqual(controller.snapshot()['global'], 1)
            lease.finish(); lease.finish()
            self.assertEqual(controller.snapshot()['global'], 0)
            controller.acquire(next_key, next_model, ['LQ50'], policy, resources).finish()

    def test_cancelled_response_holds_still_running_sync_upstream(self):
        async def exercise():
            controller = AdmissionController()
            started, finish = threading.Event(), threading.Event()
            def block(): started.set(); finish.wait(3)
            async def app(scope, receive, send):
                scope['admission_lease'] = controller.acquire('key', 'model', [], {}, {})
                await upstream_thread(block)
            run = asyncio.create_task(AdmissionLifetimeMiddleware(app)({}, None, None))
            await asyncio.wait_for(asyncio.to_thread(started.wait), 2)
            run.cancel()
            with self.assertRaises(asyncio.CancelledError): await run
            self.assertEqual(controller.snapshot()['global'], 1)
            finish.set()
            for _ in range(100):
                if controller.snapshot()['global'] == 0: break
                await asyncio.sleep(.01)
            self.assertEqual(controller.snapshot()['global'], 0)
        asyncio.run(exercise())


if __name__ == '__main__': unittest.main()
