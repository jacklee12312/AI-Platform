from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class AdminAuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from hash_password import encode

        cls.temp = tempfile.TemporaryDirectory()
        os.environ["ADMIN_PASSWORD_HASH"] = encode("modeldeck-test-password")
        os.environ["ADMIN_AGENT_TOKEN"] = "agent-test-token-value"
        os.environ["HOUMO_API_KEY"] = "gateway-test-token-value"
        os.environ["GATEWAY_MANAGEMENT_TOKEN"] = "gateway-management-token-value"
        os.environ["ADMIN_DATA_DIR"] = cls.temp.name
        os.environ["ADMIN_STATIC_DIR"] = str(ROOT / "frontend" / "dist")
        sys.modules.pop("admin_app.main", None)
        cls.module = importlib.import_module("admin_app.main")
        cls.client_context = TestClient(cls.module.app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        cls.temp.cleanup()

    def test_login_session_and_csrf(self):
        wrong = self.client.post("/api/login", json={"password": "wrong-password"})
        self.assertEqual(wrong.status_code, 401)

        login = self.client.post("/api/login", json={"password": "modeldeck-test-password"})
        self.assertEqual(login.status_code, 200)
        csrf = login.json()["csrf"]
        self.assertTrue(csrf)

        session = self.client.get("/api/session")
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.json()["username"], "admin")

        missing_csrf = self.client.post("/api/logout")
        self.assertEqual(missing_csrf.status_code, 403)
        logout = self.client.post("/api/logout", headers={"X-CSRF-Token": csrf})
        self.assertEqual(logout.status_code, 200)
        anonymous = self.client.get("/api/session")
        self.assertEqual(anonymous.status_code, 200)
        self.assertFalse(anonymous.json()["authenticated"])

    def test_security_headers_and_spa(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])

    def test_gateway_management_token_is_separate_from_business_key(self):
        self.assertEqual(
            self.module._gateway_headers(),
            {"X-Gateway-Admin-Token": "gateway-management-token-value"},
        )
        health = self.client.get("/health/live")
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()["ok"])

    def test_explicit_agent_denial_reaches_ui_without_ambiguous_502(self):
        login = self.client.post('/api/login', json={'password': 'modeldeck-test-password'})
        with mock.patch.object(self.module, '_agent', new=mock.AsyncMock(side_effect=self.module.AgentRequestError(409, 'inactive model'))):
            response = self.client.post('/api/services/qwen38/restart', headers={'X-CSRF-Token': login.json()['csrf']})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.module.AgentRequestError(403, 'agent credentials invalid').status_code, 502)

    def test_console_test_forwards_request_id_for_reconciliation(self):
        login = self.client.post('/api/login', json={'password':'modeldeck-test-password'})
        with mock.patch.object(self.module, '_gateway', new=mock.AsyncMock(return_value={'choices':[]})) as gateway:
            response = self.client.post('/api/test/chat', headers={'X-Request-ID':'console-test-id', 'X-CSRF-Token':login.json()['csrf']}, json={'model':'copaw-flash-9b','prompt':'test','stream':False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(gateway.call_args.kwargs['request_id'], 'console-test-id')

    def test_device_recovery_requires_csrf_confirmation_and_is_audited(self):
        login = self.client.post("/api/login", json={"password": "modeldeck-test-password"})
        headers = {"X-CSRF-Token": login.json()["csrf"]}
        payload = {"mode": "multi-model", "confirmation": "恢复设备"}
        self.assertEqual(self.client.post("/api/device/recover", json=payload).status_code, 403)
        self.assertEqual(self.client.post("/api/device/recover", headers=headers,
                                         json={**payload, "confirmation": "yes"}).status_code, 422)
        job = {"id": "d" * 32, "status": "succeeded", "kind": "device-recovery", "target": "houmo-xh2",
               "result": {"evidence_id": "fixture-recovery", "probes": [{"passed": True}]}}
        with mock.patch.object(self.module, "_agent", new=mock.AsyncMock(return_value=job)) as agent:
            response = self.client.post("/api/device/recover", headers=headers, json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(agent.call_args.args, ("POST", "/v1/device-recovery"))
        records = self.client.get("/api/audit?job_id=" + job["id"]).json()["data"]
        self.assertEqual(records[0]["action"], "device-recovery")
        self.assertEqual(records[0]["status"], "succeeded")


class AgentConfigurationTests(unittest.TestCase):
    def setUp(self):
        os.environ["ADMIN_AGENT_TOKEN"] = "agent-test-token-value"
        sys.modules.pop("agent.main", None)
        self.module = importlib.import_module("agent.main")
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        gateway = root / "gateway"
        houmo = root / "houmo"
        sensevoice = root / "sensevoice"
        (gateway / "config").mkdir(parents=True)
        houmo.mkdir()
        sensevoice.mkdir()
        self.module.GATEWAY_DIR = gateway
        self.module.MODEL_REGISTRY = gateway / "config" / "models.json"
        self.module.GATEWAY_COMPOSE = gateway / "compose.yaml"
        self.module.HOUMO_DEPLOY_DIR = houmo
        self.module.HOUMO_COMPOSE = houmo / "compose.yaml"
        self.module.SENSEVOICE_DIR = sensevoice
        self.module.SENSEVOICE_COMPOSE = sensevoice / "compose.yaml"

        defaults = self.module.RuntimeSettings()
        values = defaults.model_dump()
        bindings = self.module.prepare_runtime_texts.__globals__["ALL_BINDINGS"]
        for service, path in (
            ("gateway", self.module.GATEWAY_COMPOSE),
            ("guard", self.module.HOUMO_COMPOSE),
            ("sensevoice", self.module.SENSEVOICE_COMPOSE),
        ):
            lines = ["services:", f"  {service}:", "    environment:"]
            lines.extend(
                f'      {binding.env_key}: "{binding.encode(values[binding.setting])}"'
                for binding in bindings
                if binding.service == service
            )
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.module.MODEL_REGISTRY.write_text(
            json.dumps(
                {
                    "services": {"gateway_version": "0.4.0"},
                    "models": [
                        {"id": "chat", "capability": "chat", "enabled": True, "guard": True, "base_url": "http://chat:17701"},
                        {"id": "embedding", "capability": "embeddings", "enabled": True, "guard": False, "base_url": "http://embed:17702"},
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_prepare_registry_limits_editable_fields(self):
        model = self.module.ConfigModel(id="chat", enabled=False, guard=False, base_url="http://chat-next:17711")
        document, changes = self.module._prepare_registry([model])
        self.assertEqual(len(changes), 1)
        self.assertFalse(document["models"][0]["enabled"])
        self.assertEqual(document["models"][0]["capability"], "chat")

    def test_rejects_credentials_and_non_chat_guard(self):
        with self.assertRaises(ValueError):
            self.module._prepare_registry([
                self.module.ConfigModel(id="chat", enabled=True, guard=True, base_url="http://user:pass@chat:17701")
            ])
        with self.assertRaises(ValueError):
            self.module._prepare_registry([
                self.module.ConfigModel(id="embedding", enabled=True, guard=True, base_url="http://embed:17702")
            ])

    def test_disabled_model_may_have_empty_url(self):
        document = json.loads(self.module.MODEL_REGISTRY.read_text(encoding="utf-8"))
        document["models"][1]["enabled"] = False
        document["models"][1]["base_url"] = None
        self.module.MODEL_REGISTRY.write_text(json.dumps(document), encoding="utf-8")
        _, changes = self.module._prepare_registry([
            self.module.ConfigModel(id="embedding", enabled=False, guard=False, base_url="")
        ])
        self.assertEqual(changes, [])

    def test_runtime_guard_mode_updates_gateway_only(self):
        settings = self.module.RuntimeSettings(guard_mode="audit")
        request = self.module.ConfigRequest(
            settings=settings,
            models=[
                self.module.ConfigModel(id="chat", enabled=True, guard=True, base_url="http://chat:17701"),
            ],
        )
        _, texts, changes, affected = self.module._prepare_config(request)
        self.assertIn('GUARD_MODE: "audit"', texts["gateway"])
        self.assertEqual(affected, {"gateway"})
        self.assertIn("guard_mode", [change["id"] for change in changes])

    def test_runtime_settings_default_guard_mode_is_off(self):
        request = self.module.ConfigRequest(
            settings=self.module.RuntimeSettings(),
            models=[
                self.module.ConfigModel(id="chat", enabled=True, guard=True, base_url="http://chat:17701"),
            ],
        )
        self.assertEqual(request.settings.guard_mode, "off")

    def test_prepare_runtime_configuration_targets_all_affected_services(self):
        settings = self.module.RuntimeSettings(
            generation_defaults_enabled=True,
            default_max_tokens=256,
            guard_cpu_threads=6,
            guard_max_streams=3,
            asr_num_threads=6,
            asr_max_concurrency=3,
        )
        request = self.module.ConfigRequest(
            settings=settings,
            models=[self.module.ConfigModel(id="chat", enabled=True, guard=True, base_url="http://chat:17701")],
        )
        _, texts, changes, affected = self.module._prepare_config(request)
        self.assertEqual(affected, {"gateway", "guard", "sensevoice"})
        self.assertIn('DEFAULT_MAX_TOKENS: "256"', texts["gateway"])
        self.assertIn('GUARD_MAX_STREAMS: "3"', texts["guard"])
        self.assertIn('SENSEVOICE_MAX_CONCURRENCY: "3"', texts["sensevoice"])
        self.assertEqual({change["group"] for change in changes}, {"gateway", "guard", "sensevoice"})

    def test_runtime_relationship_validation_rejects_unsafe_concurrency(self):
        with self.assertRaises(ValueError):
            self.module.RuntimeSettings(guard_cpu_threads=2, guard_max_streams=3)
        with self.assertRaises(ValueError):
            self.module.RuntimeSettings(asr_num_threads=1, asr_max_concurrency=2)

    def test_config_schema_covers_every_runtime_setting(self):
        schema_fields = {
            field["id"]
            for group in self.module.CONFIG_SCHEMA["groups"]
            for field in group["fields"]
        }
        self.assertEqual(schema_fields, set(self.module.RuntimeSettings.model_fields))

    def test_removed_ocr_mode_is_rejected(self):
        with self.assertRaises(self.module.HTTPException) as raised:
            asyncio.run(self.module.switch_mode(self.module.ModeRequest(mode="ocr")))
        self.assertEqual(raised.exception.status_code, 400)

    def test_inactive_model_restart_is_rejected(self):
        status = {
            "configured_mode": "multi-model",
            "detected_mode": "multi-model",
        }
        with mock.patch.object(self.module, "_modelctl_status", return_value=status):
            with self.assertRaises(self.module.HTTPException) as raised:
                asyncio.run(self.module.restart_service("qwen38"))
        self.assertEqual(raised.exception.status_code, 409)

    def test_active_model_restart_is_accepted(self):
        status = {
            "configured_mode": "multi-model",
            "detected_mode": "multi-model",
        }
        queued = {"id": "job-test", "status": "queued"}
        with (
            mock.patch.object(self.module, "_modelctl_status", return_value=status),
            mock.patch.object(self.module, "_start_job", return_value=queued),
        ):
            result = asyncio.run(self.module.restart_service("glm-ocr"))
        self.assertEqual(result, queued)

    def test_failed_apply_restores_every_configuration_file(self):
        paths = (
            self.module.MODEL_REGISTRY,
            self.module.GATEWAY_COMPOSE,
            self.module.HOUMO_COMPOSE,
            self.module.SENSEVOICE_COMPOSE,
        )
        before = {path: path.read_bytes() for path in paths}
        request = self.module.ConfigRequest(
            settings=self.module.RuntimeSettings(
                generation_defaults_enabled=True,
                guard_cpu_threads=6,
                guard_max_streams=3,
                asr_num_threads=6,
                asr_max_concurrency=3,
            ),
            models=[self.module.ConfigModel(id="chat", enabled=False, guard=False, base_url="http://chat:17701")],
        )
        with mock.patch.object(
            self.module,
            "_recreate_services",
            side_effect=[RuntimeError("simulated health failure"), None],
        ):
            with self.assertRaises(RuntimeError):
                self.module._apply_config(request)
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        manifests = list((self.module.MODEL_REGISTRY.parent / "backups").glob("config.*.json"))
        self.assertEqual(len(manifests), 1)

    @unittest.skipIf(os.name == "nt", "POSIX ownership and mode semantics")
    def test_atomic_write_preserves_file_mode(self):
        target = Path(self.temp.name) / "mode-test.yaml"
        target.write_text("before\n", encoding="utf-8")
        target.chmod(0o644)
        before = target.stat()
        self.module._write_text_atomic(target, "after\n")
        after = target.stat()
        self.assertEqual(after.st_mode & 0o777, before.st_mode & 0o777)
        self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))


if __name__ == "__main__":
    unittest.main()
