from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("startup_verify", ROOT / "startup_verify.py")
startup = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(startup)


class StartupVerifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.deploy = root / "deploy"
        (self.deploy / "state").mkdir(parents=True)
        (self.deploy / "state" / "active-mode").write_text("copaw\n", encoding="utf-8")
        self.boot = root / "boot_id"
        self.boot.write_text("boot-one\n", encoding="utf-8")
        self.state = self.deploy / "state" / "startup-verification.json"
        self.patchers = [
            mock.patch.object(startup, "DEPLOY", self.deploy),
            mock.patch.object(startup, "STATE", self.state),
            mock.patch.object(startup, "BOOT_ID", self.boot),
            mock.patch.object(startup, "agent_connection", return_value=("http://agent", "token")),
        ]
        for patcher in self.patchers:
            patcher.start(); self.addCleanup(patcher.stop)

    @staticmethod
    def host(verified):
        return {"runtime_safety": {"incident": None, "maintenance": None, "monitor_error": None,
                "services": {"houmo-copaw": {"running": True, "health": "healthy", "verified": verified}}}}

    def test_success_is_not_repeated_on_same_boot(self):
        startup.atomic_write({"schema_version": 1, "boot_id": "boot-one", "status": "succeeded",
                              "verified_models": ["houmo-copaw"]})
        with mock.patch.object(startup, "request", return_value=(200, self.host(True))) as request:
            self.assertEqual(startup.main([]), 0)
        self.assertEqual(request.call_count, 1)

    def test_success_is_not_reused_when_expected_running_set_changed(self):
        startup.atomic_write({"schema_version": 1, "boot_id": "boot-one", "status": "succeeded",
                              "verified_models": ["houmo-copaw", "houmo-bge-m3"]})
        with mock.patch.object(startup, "request", return_value=(200, self.host(True))):
            with self.assertRaisesRegex(RuntimeError, "实例.*变化"):
                startup.main([])

    def test_failed_boot_is_not_automatically_retried(self):
        startup.atomic_write({"schema_version": 1, "boot_id": "boot-one", "status": "failed"})
        with mock.patch.object(startup, "request", return_value=(200, self.host(False))):
            with self.assertRaisesRegex(RuntimeError, "不会自动重试"):
                startup.main([])

    def test_success_persists_progress_and_verified_models(self):
        final = {"status": "succeeded", "progress": {"stage": "完成", "completed": 1, "total": 1},
                 "result": {"expected_models": ["houmo-copaw"], "already_verified": False,
                            "verification": {"status": "succeeded", "evidence_id": "evidence",
                                             "verified_instances": {"houmo-copaw": ["identity"]}}}}
        responses = [(200, self.host(False)), (200, {"id": "job", "status": "queued"}), (200, final)]
        with mock.patch.object(startup, "request", side_effect=responses), mock.patch.object(startup.time, "sleep"):
            self.assertEqual(startup.main(["--force", "--source", "manual"]), 0)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "succeeded")
        self.assertEqual(saved["verified_models"], ["houmo-copaw"])
        self.assertEqual(saved["evidence_id"], "evidence")


if __name__ == "__main__":
    unittest.main()
