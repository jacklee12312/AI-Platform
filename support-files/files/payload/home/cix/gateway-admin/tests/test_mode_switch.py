from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.device_recovery import RecoveryFailure


class Safety:
    def __init__(self, snapshots=None):
        self.snapshots = list(snapshots or [{"incident": None, "maintenance": None, "monitor_error": None}])

    def snapshot(self):
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]


class Verifier:
    def __init__(self, result=None, error=None):
        self.result = result or {"status": "succeeded", "verified_instances": {"fixture": ["id"]}}
        self.error = error

    def verify_current(self, mode):
        if self.error:
            raise self.error
        return {**self.result, "mode": mode}


class ModeSwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("agent.main")

    def status(self, mode="multi-model"):
        return {"healthy": True, "configured_mode": mode, "detected_mode": mode, "services": {}}

    def test_progress_parser_reports_the_model_being_loaded(self):
        value = self.module._mode_line_progress("2026-09-09 start one service: qwen-vl; remaining=200s")
        self.assertEqual(value["phase"], "loading")
        self.assertEqual(value["current_model"], "qwen3-vl-8b")

    def test_success_requires_functional_verification(self):
        progress = []
        with mock.patch.object(self.module, "RUNTIME_SAFETY", Safety()), \
             mock.patch.object(self.module, "_modelctl_status", return_value=self.status()), \
             mock.patch.object(self.module, "_stream_modelctl_switch", return_value=CompletedProcess([], 0, "complete", "")), \
             mock.patch.object(self.module, "Recovery", return_value=Verifier()) as factory:
            result = self.module._mode_switch_transaction("qwen38", progress.append)
        self.assertEqual(result["verification"]["status"], "succeeded")
        self.assertEqual(progress[-1]["phase"], "complete")

    def test_existing_incident_rejects_switch_before_model_mutation(self):
        safety = Safety([{"incident": {"id": "existing-fatal"}, "maintenance": None, "monitor_error": None}])
        with mock.patch.object(self.module, "RUNTIME_SAFETY", safety), \
             mock.patch.object(self.module, "_modelctl_status") as status, \
             mock.patch.object(self.module, "_stream_modelctl_switch") as switched:
            with self.assertRaisesRegex(RuntimeError, "NPU 已隔离"):
                self.module._mode_switch_transaction("qwen38", lambda _: None)
        status.assert_not_called()
        switched.assert_not_called()

    def test_failed_target_verification_rolls_back_and_job_stays_failed(self):
        target_result = {"status": "failed", "failed_stage": "固定推理", "first_fault": None}
        target = Verifier(error=RecoveryFailure("答案不正确", target_result))
        rollback = Verifier()
        progress = []
        with mock.patch.object(self.module, "RUNTIME_SAFETY", Safety()), \
             mock.patch.object(self.module, "_modelctl_status", return_value=self.status()), \
             mock.patch.object(self.module, "_stream_modelctl_switch", side_effect=[CompletedProcess([], 0, "target", ""), CompletedProcess([], 0, "rollback", "")]) as switched, \
             mock.patch.object(self.module, "Recovery", side_effect=[target, rollback]), \
             mock.patch.object(self.module, "_run") as stopped:
            with self.assertRaises(self.module.ModeSwitchFailure) as ctx:
                self.module._mode_switch_transaction("qwen38", progress.append)
        self.assertEqual(switched.call_count, 2)
        self.assertEqual(ctx.exception.result["rollback"]["status"], "succeeded")
        self.assertIn("已回滚", str(ctx.exception))
        stopped.assert_not_called()

    def test_fatal_target_failure_stops_device_models_without_unsafe_reload(self):
        safety = Safety([
            {"incident": None, "maintenance": None, "monitor_error": None},
            {"incident": {"id": "fatal"}, "maintenance": None, "monitor_error": None},
        ])
        target = Verifier(error=RecoveryFailure("NPU fatal", {"status": "failed", "first_fault": {"source": "copaw"}}))
        with mock.patch.object(self.module, "RUNTIME_SAFETY", safety), \
             mock.patch.object(self.module, "_modelctl_status", return_value=self.status()), \
             mock.patch.object(self.module, "_stream_modelctl_switch", return_value=CompletedProcess([], 0, "target", "")) as switched, \
             mock.patch.object(self.module, "Recovery", return_value=target), \
             mock.patch.object(self.module, "_run", return_value=CompletedProcess([], 0, "", "")) as stopped:
            with self.assertRaises(self.module.ModeSwitchFailure) as ctx:
                self.module._mode_switch_transaction("qwen38", lambda _: None)
        self.assertEqual(switched.call_count, 1)
        stopped.assert_called_once_with([self.module.MODELCTL, "stop"], timeout=120)
        self.assertEqual(ctx.exception.result["rollback"]["status"], "blocked")
        self.assertEqual(ctx.exception.result["safe_state"], "all_device_models_stopped")


if __name__ == "__main__":
    unittest.main()
