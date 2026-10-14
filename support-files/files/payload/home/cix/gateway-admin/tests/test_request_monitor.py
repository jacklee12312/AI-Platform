import time
from unittest import mock

from fastapi.testclient import TestClient

from admin_app import main as admin


def _session(monkeypatch):
    monkeypatch.setattr(
        admin,
        "SESSIONS",
        {"unit-session": {"expires_at": time.time() + 60, "csrf": "unit-csrf"}},
    )
    client = TestClient(admin.app)
    client.cookies.set("modeldeck_session", "unit-session")
    return client


def test_request_monitor_proxy_requires_session_and_validates_internal_id(monkeypatch):
    proxy = mock.AsyncMock(return_value={"id": "a" * 32, "requests": []})
    monkeypatch.setattr(admin, "_gateway", proxy)
    client = TestClient(admin.app)
    assert client.get("/api/requests/active").status_code == 401
    assert client.get("/api/requests/" + "a" * 32).status_code == 401
    proxy.assert_not_called()

    client = _session(monkeypatch)
    assert client.get("/api/requests/invalid").status_code == 400
    for path in ("/api/requests/active", "/api/requests/" + "a" * 32):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
    assert [call.args[1] for call in proxy.call_args_list] == [
        "/internal/requests/active",
        "/internal/requests/" + "a" * 32,
    ]


def test_request_cancel_requires_csrf_and_writes_audit(monkeypatch):
    proxy = mock.AsyncMock(
        return_value={"status": "cancelling", "message": "pending"}
    )
    audit = mock.AsyncMock()
    monkeypatch.setattr(admin, "_gateway", proxy)
    monkeypatch.setattr(admin, "_audit_request", audit)
    client = _session(monkeypatch)
    url = "/api/requests/" + "a" * 32 + "/cancel"
    assert client.post(url).status_code == 403
    proxy.assert_not_called()
    response = client.post(url, headers={"X-CSRF-Token": "unit-csrf"})
    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    assert audit.call_args.args[1:4] == (
        "request-cancel",
        "a" * 32,
        "cancelling",
    )
