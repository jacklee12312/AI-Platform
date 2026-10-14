"""Real local process restarts with simulated operations; never contacts a box."""
import contextlib
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx


class AuditProcessRestartTests(unittest.TestCase):
    def test_completed_and_interrupted_jobs_survive_real_admin_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as socket_:
                socket_.bind(("127.0.0.1", 0))
                port = socket_.getsockname()[1]
            base = f"http://127.0.0.1:{port}"

            @contextlib.contextmanager
            def server():
                process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).with_name("serve_audit_fixture.py")),
                     "--data-dir", directory, "--port", str(port)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                try:
                    with httpx.Client(base_url=base, timeout=2, trust_env=False) as client:
                        deadline = time.monotonic() + 15
                        while True:
                            self.assertIsNone(process.poll(), "fixture exited during startup")
                            try:
                                if client.get("/health/live").status_code == 200:
                                    break
                            except httpx.TransportError:
                                pass
                            if time.monotonic() >= deadline:
                                self.fail("fixture did not become ready")
                            time.sleep(0.05)
                        login = client.post("/api/login", json={"password": "audit-test-password"})
                        client.headers["X-CSRF-Token"] = login.json()["csrf"]
                        yield client
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

            def wait_status(client, request_id, expected):
                deadline = time.monotonic() + 5
                while True:
                    rows = client.get("/api/audit", params={"request_id": request_id}).json()["data"]
                    if rows and rows[0]["status"] == expected:
                        return rows[0]
                    if time.monotonic() >= deadline:
                        self.fail(f"expected {expected}, got {rows}")
                    time.sleep(0.05)

            with server() as client:
                done_job = client.post("/api/services/copaw/restart", headers={"X-Request-ID": "completed"}).json()["id"]
                before = wait_status(client, "completed", "succeeded")
                client.post("/api/services/copaw/restart", headers={"X-Request-ID": "interrupted"})
            # Killing the process also clears the fixture agent's in-memory jobs.
            with server() as client:
                after = wait_status(client, "completed", "succeeded")
                self.assertEqual(before, after)
                wait_status(client, "interrupted", "unknown")
                result = client.get(f"/api/jobs/{done_job}")
                self.assertEqual(result.status_code, 200)
                self.assertTrue(result.json()["from_audit"])
                self.assertEqual(result.json()["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
