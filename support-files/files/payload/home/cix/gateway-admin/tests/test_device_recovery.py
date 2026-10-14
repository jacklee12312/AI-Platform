from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import device_recovery as recovery


class FakeRecovery(recovery.Recovery):
    def __init__(self, root, fail_at=""):
        super().__init__(Path(root))
        self.calls = []
        self.fail_at = fail_at

    def prepare(self, mode):
        self.root.mkdir(parents=True)
        self.names = list(recovery.DEVICE)
        self.mode = mode
        self.calls.append("prepare")

    def snapshot(self, name):
        self.calls.append("snapshot:" + name)

    def stop_all(self):
        self.calls.append("stop_all")

    def start(self, names):
        self.check()
        self.calls.append(("start", names))

    def wait_ready(self, name, timeout=360):
        self.calls.append(("ready", name))
        if self.fail_at == name:
            self.mark_fault(name, "SyncYieldErr:12 ERR_FATAL")
        self.check()

    def inspect(self, names, guarded=True):
        running = set(recovery.MULTI if self.mode == "multi-model" else ("houmo-qwen38-27b",))
        return [{"Name": "/" + name, "Id": name + "-fixture", "RestartCount": 0,
                 "State": {"Running": name in running, "StartedAt": "2026-09-09T00:00:00Z",
                           "Health": {"Status": "healthy"}},
                 "Config": {"Env": []}, "NetworkSettings": {"Networks": {}}} for name in names]

    def probe(self, name):
        self.check()
        self.calls.append(("probe", name))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.lock = mock.patch.object(recovery, "device_lock", lambda _: nullcontext())
        self.lock.start()

    def tearDown(self):
        self.lock.stop()
        self.temp.cleanup()

    def test_baseline_only_starts_copaw_after_all_stop(self):
        engine = FakeRecovery(self.temp.name)
        result = engine.run("multi-model", "baseline")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(engine.calls[:3], ["prepare", "stop_all", ("start", ["houmo-copaw"])])
        self.assertEqual([v for v in engine.calls if isinstance(v, tuple) and v[0] == "start"], [("start", ["houmo-copaw"])])

    def test_parallel_plan_starts_same_five_models(self):
        engine = FakeRecovery(self.temp.name)
        engine.run("multi-model", "parallel")
        self.assertEqual(engine.calls[2], ("start", list(recovery.MULTI)))

    def test_recovery_validates_primary_before_remaining_models(self):
        engine = FakeRecovery(self.temp.name)
        engine.run("multi-model")
        probe = engine.calls.index(("probe", "houmo-copaw"))
        next_start = engine.calls.index(("start", ["houmo-qwen3-vl-8b"]))
        self.assertLess(probe, next_start)
        self.assertEqual(engine.calls.count(("probe", "houmo-copaw")), 2)

    def test_first_fatal_stops_everything_without_retry_or_probe(self):
        engine = FakeRecovery(self.temp.name, "houmo-copaw")
        with self.assertRaises(recovery.RecoveryFailure) as ctx:
            engine.run("multi-model")
        self.assertEqual(ctx.exception.result["safe_state"], "all_device_models_stopped")
        self.assertEqual(engine.calls.count("stop_all"), 2)
        self.assertFalse(any(v[0] == "probe" for v in engine.calls if isinstance(v, tuple)))
        self.assertTrue((engine.root / "first-fault.json").exists())
        self.assertEqual(len([v for v in engine.calls if isinstance(v, tuple) and v[0] == "start"]), 1)

    def test_qwen38_mode_does_not_start_copaw(self):
        engine = FakeRecovery(self.temp.name)
        engine.run("qwen38")
        self.assertIn(("start", ["houmo-qwen38-27b"]), engine.calls)
        self.assertNotIn(("start", ["houmo-copaw"]), engine.calls)

    def test_verify_current_probes_every_target_and_rechecks_copaw_after_coexistence(self):
        engine = FakeRecovery(self.temp.name)
        result = engine.verify_current("multi-model")
        probes = [value[1] for value in engine.calls if isinstance(value, tuple) and value[0] == "probe"]
        self.assertEqual(probes, [*recovery.MULTI, "houmo-copaw"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["plan"], "verify-current")

    def test_no_mutation_if_log_preservation_fails(self):
        engine = FakeRecovery(self.temp.name)
        with mock.patch.object(engine, "prepare", side_effect=RuntimeError("capture failed")):
            with self.assertRaises(recovery.RecoveryFailure):
                engine.run("multi-model")
        self.assertNotIn("stop_all", engine.calls)

    def test_rejects_unknown_plan_and_stale_mode(self):
        engine = FakeRecovery(self.temp.name)
        with self.assertRaises(ValueError):
            engine.run("qwen38", "parallel")
        real = recovery.Recovery(Path(self.temp.name))
        (Path(self.temp.name) / "state").mkdir(exist_ok=True)
        (Path(self.temp.name) / "state" / "active-mode").write_text("qwen38")
        with self.assertRaisesRegex(RuntimeError, "运行模式已变化"):
            real.prepare("multi-model")

    def test_fatal_patterns_and_first_evidence_are_stable(self):
        engine = FakeRecovery(self.temp.name)
        engine.prepare("multi-model")
        engine.mark_fault("kernel", "group 0 exec timeout!")
        engine.mark_fault("copaw", "ERR_FATAL")
        self.assertEqual(engine.first_fault["source"], "kernel")
        self.assertTrue(recovery.FATAL.search("xh2a_ipu_sync_group failed! ret:16"))
        self.assertFalse(recovery.FATAL.search("model loaded; server is listening"))
        self.assertTrue(recovery.fatal_record(
            "xh2a_ipu_sync_group failed! ret:16 result:0 group_id:70 timeout_us:100000000"
        ))
        self.assertFalse(recovery.fatal_record(
            "xh2a_ipu_sync_group failed! ret:4 result:0 hal_errno:4 "
            "group_id:24340 device_id:0 timeout_us:5000000"
        ))

    def test_agent_rejects_concurrent_job(self):
        module = importlib.import_module("agent.main")
        release = threading.Event()
        first = module._start_job("test", "x", lambda: release.wait(3) or {})
        try:
            with self.assertRaises(module.HTTPException) as ctx:
                module._start_job("device-recovery", "houmo-xh2", lambda: {})
            self.assertEqual(ctx.exception.status_code, 409)
        finally:
            release.set()
            # Wait on the same mutex, not a guessed sleep.
            with module.OPERATION_LOCK:
                pass
        self.assertEqual(module.JOBS[first["id"]]["status"], "succeeded")

    def test_confirmation_and_mode_are_validated(self):
        module = importlib.import_module("agent.main")
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            module.RecoveryRequest(mode="multi-model", confirmation="yes")
        with self.assertRaises(ValidationError):
            module.RecoveryRequest(mode="anything", confirmation="恢复设备")

    def test_log_collector_exception_triggers_fail_closed(self):
        engine = FakeRecovery(self.temp.name)
        engine.prepare("multi-model")
        with mock.patch.object(engine, "monitor", side_effect=OSError("no collector")):
            engine.monitored_logs("kernel", recovery.now(), True)
        self.assertTrue(engine.fatal.is_set())
        self.assertEqual(engine.first_fault["source"], "log-capture")


if __name__ == "__main__":
    unittest.main()
