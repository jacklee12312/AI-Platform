from __future__ import annotations

import asyncio
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from admin_app.audit import AuditStore, redact


class AuditPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "audit.db"
        self.store = AuditStore(self.path, ("private-agent-value",))
        self.store.initialize()

    def test_write_survives_new_connection_and_process(self):
        self.store.write("login", "local", "succeeded", "request-1")
        self.assertEqual(AuditStore(self.path).read()[0]["request_id"], "request-1")
        output = subprocess.check_output([
            sys.executable, "-c",
            "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute('SELECT count(*) FROM audit').fetchone()[0])",
            str(self.path),
        ], text=True)
        self.assertEqual(output.strip(), "1")

    def test_failed_transaction_is_rolled_back_and_connection_closed(self):
        with self.assertRaises(RuntimeError):
            with self.store.connection() as connection:
                connection.execute("INSERT INTO audit VALUES ('x','now','admin','login','local','failed','r','','','now')")
                raise RuntimeError("abort")
        self.assertEqual(self.store.read(), [])
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_migration_preserves_legacy_records_and_recovers_pending_jobs(self):
        old = Path(self.temp.name) / "old.db"
        with sqlite3.connect(old) as connection:
            connection.execute("CREATE TABLE audit (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, actor TEXT NOT NULL, "
                               "action TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL, request_id TEXT NOT NULL, detail TEXT NOT NULL)")
            connection.execute("INSERT INTO audit VALUES (?,?,?,?,?,?,?,?)",
                               ("old", "2026-09-04", "admin", "restart", "copaw", "accepted", "old-request", '{"job_id":"old-job"}'))
        connection.close()
        store = AuditStore(old)
        store.initialize()
        store.initialize()
        self.assertEqual(store.pending(), ["old-job"])
        self.assertEqual(store.read()[0]["id"], "old")
        store.write("login", "local", "failed", "new-request")
        self.assertEqual(len(store.read()), 2)

    def test_concurrent_writes_are_all_committed(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda n: self.store.write("login", "local", "succeeded", f"r-{n}"), range(40)))
        self.assertEqual(len(self.store.read()), 40)

    def test_terminal_result_is_idempotent_and_never_regresses(self):
        self.store.write("restart", "copaw", "accepted", "r", {"job_id": "job-1"}, job_id="job-1")
        succeeded = {"id": "job-1", "status": "succeeded", "finished_at": "finished", "result": {"service": "copaw"}}
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.store.record_job(succeeded), range(10)))
        self.store.record_job({"id": "job-1", "status": "running"})
        rows = self.store.read(job_id="job-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "succeeded")
        self.assertEqual(json.loads(rows[0]["detail"])["job"]["finished_at"], "finished")
        self.assertEqual(self.store.pending(), [])

    def test_running_does_not_regress_to_queued(self):
        self.store.write("restart", "copaw", "accepted", "r", {}, job_id="job-1")
        self.store.record_job({"id": "job-1", "status": "running"})
        self.store.record_job({"id": "job-1", "status": "queued"})
        self.assertEqual(self.store.read()[0]["status"], "running")

    def test_redaction_and_long_details_remain_valid_json(self):
        detail = {"password": "do-not-store", "nested": {"api_key": "business-secret", "max_tokens": 128},
                  "error": "private-agent-value Bearer business-secret https://user:secret@box/ --api-key business-secret"}
        self.store.write("config-apply", "runtime", "failed", "r", detail)
        row = self.store.read()[0]
        for secret in ("do-not-store", "business-secret", "private-agent-value", "user:secret"):
            self.assertNotIn(secret, row["detail"])
        self.assertEqual(json.loads(row["detail"])["nested"]["max_tokens"], 128)
        self.store.write("restart", "copaw", "failed", "long", {"error": "x" * 20000})
        self.assertTrue(json.loads(self.store.read(request_id="long")[0]["detail"])["truncated"])
        self.store.write("restart", "copaw", "failed", "escaped", {"error": '"' * 20000})
        self.assertLessEqual(len(self.store.read(request_id="escaped")[0]["detail"]), 8000)

    def test_filters_are_parameterized_and_searches_are_literal(self):
        self.store.write("restart", "copaw", "accepted", "a", {}, job_id="job-a")
        self.store.write("login", "local", "failed", "b")
        self.assertEqual(len(self.store.read(request_id="a", job_id="job-a")), 1)
        self.assertEqual(len(self.store.read(q="CoPaw")), 1)
        self.assertEqual(self.store.read(q="%"), [])
        self.assertEqual(self.store.read(request_id="' OR 1=1 --"), [])


class AuditAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        from hash_password import encode
        self.env = mock.patch.dict(os.environ, {
            "ADMIN_DATA_DIR": self.temp.name, "ADMIN_PASSWORD_HASH": encode("audit-test-password"),
            "ADMIN_AGENT_TOKEN": "private-agent-value", "GATEWAY_MANAGEMENT_TOKEN": "private-management-value",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        sys.modules.pop("admin_app.main", None)
        self.module = importlib.import_module("admin_app.main")
        self.agent = mock.AsyncMock(side_effect=AssertionError("unexpected external call"))
        patcher = mock.patch.object(self.module, "_agent", self.agent)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.module.AUDIT_POLL_SECONDS = 0.02
        self.client = TestClient(self.module.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.csrf = self.client.post("/api/login", json={"password": "audit-test-password"}).json()["csrf"]

    def post(self, path, payload=None, request_id="operation-request"):
        return self.client.post(path, json=payload, headers={"X-CSRF-Token": self.csrf, "X-Request-ID": request_id})

    def rows(self, **filters):
        response = self.client.get("/api/audit", params=filters)
        self.assertEqual(response.status_code, 200)
        return response.json()["data"]

    def test_login_success_failure_logout_and_denied_operations(self):
        self.client.post("/api/login", json={"password": "wrong-secret"}, headers={"X-Request-ID": "wrong-login"})
        row = self.rows(request_id="wrong-login")[0]
        self.assertEqual(row["status"], "failed")
        self.assertNotIn("wrong-secret", row["detail"])
        denied = self.client.post("/api/services/copaw/restart", headers={"X-Request-ID": "csrf-denied"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(len(self.rows(request_id="csrf-denied")), 1)
        self.assertEqual(self.post("/api/logout").status_code, 200)
        self.assertEqual(self.client.get("/api/audit").status_code, 401)
        self.agent.assert_not_called()

    def test_mode_restart_and_config_final_results_without_browser_polling(self):
        jobs = {}

        async def agent(method, path, **kwargs):
            if method == "POST":
                job_id = f"job-{len(jobs)}"
                status = "failed" if path == "/v1/config/apply" else "succeeded"
                jobs[job_id] = {"id": job_id, "status": status, "finished_at": "finished", "result": {"changed": True},
                                "error": "health check failed" if status == "failed" else None}
                return {"id": job_id, "status": "queued"}
            return jobs[path.rsplit("/", 1)[1]]

        self.agent.side_effect = agent
        cases = [("/api/mode/switch", {"mode": "multi-model"}, "succeeded"),
                 ("/api/services/copaw/restart", None, "succeeded"),
                 ("/api/config/apply", {"settings": {}, "models": [{"id": "copaw", "enabled": True, "base_url": "http://copaw:17701"}]}, "failed")]
        for i, (path, payload, expected) in enumerate(cases):
            with self.subTest(path=path):
                response = self.post(path, payload, request_id=f"r-{i}")
                self.assertEqual(response.status_code, 200)
                job_id = response.json()["id"]
                # No GET /api/jobs or /api/audit: the server must finish tracking alone.
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    rows = self.module._audit_store().read(job_id=job_id)
                    if rows and rows[0]["status"] == expected:
                        break
                    time.sleep(0.02)
                self.assertEqual(rows[0]["status"], expected)
                self.assertEqual(len(self.rows(job_id=job_id, request_id=f"r-{i}")), 1)

    def test_agent_rejection_is_logged_once_with_reason(self):
        self.agent.side_effect = self.module.AgentRequestError(409, "service is inactive")
        response = self.post("/api/services/qwen38/restart")
        self.assertEqual(response.status_code, 409)
        rows = self.rows(request_id="operation-request")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIn("inactive", rows[0]["detail"])

    def test_submission_timeout_is_unknown_not_a_proven_failure(self):
        self.agent.side_effect = self.module.HTTPException(502, "agent unavailable")
        response = self.post("/api/services/copaw/restart")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.rows(request_id="operation-request")[0]["status"], "unknown")

    def test_invalid_agent_job_response_is_audited(self):
        self.agent.side_effect = None
        for response in ([], {"id": "../bad", "status": "queued"}, {"id": "job", "status": "invalid"}):
            with self.subTest(response=response):
                self.agent.return_value = response
                result = self.post("/api/services/copaw/restart")
                self.assertEqual(result.status_code, 502)
        self.assertEqual({r["status"] for r in self.rows(request_id="operation-request")}, {"unknown"})

    def test_malformed_request_is_logged_without_request_body(self):
        response = self.post("/api/config/apply", {"settings": {"password": "never-log"}, "models": []})
        self.assertEqual(response.status_code, 422)
        row = self.rows(request_id="operation-request")[0]
        self.assertEqual(json.loads(row["detail"]), {"http_status": 422})
        self.agent.assert_not_called()

    def test_transient_outage_retains_pending_and_lost_job_becomes_unknown(self):
        store = self.module._audit_store()
        store.write("restart", "copaw", "accepted", "r", {}, job_id="lost-job")
        self.agent.side_effect = self.module.HTTPException(502, "temporarily unavailable")
        asyncio.run(self.module._reconcile_audit_jobs())
        self.assertEqual(store.read(job_id="lost-job")[0]["status"], "accepted")
        self.agent.side_effect = self.module.AgentRequestError(404, "job not found")
        asyncio.run(self.module._reconcile_audit_jobs())
        self.assertEqual(store.read(job_id="lost-job")[0]["status"], "unknown")
        self.assertEqual(store.pending(), [])

    def test_completed_job_can_be_read_when_agent_is_unavailable(self):
        store = self.module._audit_store()
        store.write("restart", "copaw", "accepted", "r", {}, job_id="done-job")
        store.record_job({"id": "done-job", "status": "succeeded", "result": {"service": "copaw"}})
        self.agent.side_effect = self.module.HTTPException(502, "unavailable")
        response = self.client.get("/api/jobs/done-job")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["from_audit"])
        self.assertEqual(response.json()["status"], "succeeded")

    def test_key_lifecycle_records_do_not_include_full_key(self):
        result = {"id": "key-id", "name": "test-key", "prefix": "sk-prefix", "api_key": "full-private-key"}
        with mock.patch.object(self.module, "_gateway", mock.AsyncMock(return_value=result)):
            self.assertEqual(self.post("/api/api-keys", {"name": "test-key"}).status_code, 201)
            self.assertEqual(self.client.patch("/api/api-keys/key-id", json={"enabled": False}, headers={"X-CSRF-Token": self.csrf}).status_code, 200)
            self.assertEqual(self.client.delete("/api/api-keys/key-id", headers={"X-CSRF-Token": self.csrf}).status_code, 200)
        rows = self.rows(q="api-key-")
        self.assertEqual(len(rows), 3)
        self.assertNotIn("full-private-key", json.dumps(rows))

    def test_key_policy_round_trip_clear_expiry_and_audit(self):
        policy = {"allowed_models": [], "expires_at": None, "daily_request_limit": 12}
        result = {"id": "restricted", "name": "restricted", "prefix": "sk-test", "policy": policy}
        with mock.patch.object(self.module, "_gateway", mock.AsyncMock(return_value=result)) as gateway:
            self.assertEqual(self.post("/api/api-keys", {"name": "restricted", "policy": policy}).status_code, 201)
            self.assertEqual(gateway.call_args.kwargs["json_body"]["policy"], policy)
            response = self.client.patch("/api/api-keys/restricted", json={"policy": policy},
                                         headers={"X-CSRF-Token": self.csrf, "X-Request-ID": "policy-edit"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(gateway.call_args.kwargs["json_body"], {"policy": policy})
        detail = json.loads(self.rows(request_id="policy-edit")[0]["detail"])
        self.assertEqual(detail["policy"], policy)

    def test_resource_policy_requires_session_csrf_and_audits_success_and_denial(self):
        policy = {"global_max_concurrent": 2, "pool_limits": {"LQ50": 1}, "model_limits": {}}
        with mock.patch.object(self.module, "_gateway", mock.AsyncMock(return_value={"policy": policy})) as gateway:
            self.assertEqual(self.client.get("/api/resource-policy").status_code, 200)
            gateway.reset_mock()
            denied = self.client.put("/api/resource-policy", json=policy, headers={"X-Request-ID": "resource-denied"})
            self.assertEqual(denied.status_code, 403)
            gateway.assert_not_called()
            self.assertEqual(self.rows(request_id="resource-denied")[0]["status"], "failed")
            response = self.client.put("/api/resource-policy", json=policy,
                                       headers={"X-CSRF-Token": self.csrf, "X-Request-ID": "resource-save"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(gateway.call_args.kwargs["json_body"], policy)
            self.assertEqual(json.loads(self.rows(request_id="resource-save")[0]["detail"])["policy"], policy)
            self.post("/api/logout")
            gateway.reset_mock()
            self.assertEqual(self.client.get("/api/resource-policy").status_code, 401)
            self.assertEqual(self.client.put("/api/resource-policy", json=policy).status_code, 401)
            gateway.assert_not_called()

    def test_lifespan_restart_preserves_records_and_resumes_pending_tracking(self):
        self.agent.side_effect = self.module.HTTPException(502, "offline")
        self.module._audit_store().write("mode-switch", "multi-model", "accepted", "restart-test", {}, job_id="resume-job")
        self.client.__exit__(None, None, None)
        self.agent.side_effect = None
        self.agent.return_value = {"id": "resume-job", "status": "succeeded", "result": {"mode": "multi-model"}}
        self.client.__enter__()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            rows = self.module._audit_store().read(request_id="restart-test")
            if rows[0]["status"] == "succeeded":
                break
            time.sleep(0.02)
        self.assertEqual(rows[0]["status"], "succeeded")
        self.assertTrue(self.rows(q="login"))


if __name__ == "__main__":
    unittest.main()
