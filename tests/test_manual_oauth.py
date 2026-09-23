from urllib.parse import parse_qs, urlsplit
from types import SimpleNamespace
from fastapi.testclient import TestClient
from autobuild_json.api import create_app
from tests.fakes import success_result
from tests.test_api import ORIGIN


def test_manual_callback_once_and_private(settings):
    calls = []
    def complete(grant, transport):
        calls.append(grant.code)
        return success_result().record
    with TestClient(create_app(settings, token_service=SimpleNamespace(complete=complete)), base_url=ORIGIN) as client:
        login = client.post("/api/session", headers={"Origin":ORIGIN}, json={"token":"test-admin-token"})
        client.headers.update({"Origin":ORIGIN,"X-CSRF-Token":login.json()["csrf_token"]})
        response = client.post("/api/oauth/links", json={"email":"u@example.com"})
        assert response.status_code == 201
        session = response.json()
        assert "verifier" not in repr(session)
        authorize = parse_qs(urlsplit(session["desktop_url"]).query)["authorize_url"][0]
        state = parse_qs(urlsplit(authorize).query)["state"][0]
        payload = {"session_id":session["session_id"], "callback_url":f"http://localhost:1455/auth/callback?state={state}&code=fake-code"}
        result = client.post("/api/oauth/complete", json=payload)
        assert result.status_code == 200
        assert result.json()[0]["tokens"]["refresh_token"] == "fake-refresh"
        assert "attachment" in result.headers["content-disposition"]
        assert client.post("/api/oauth/complete", json=payload).status_code == 400
        assert calls == ["fake-code"]


def test_timeout_callback_is_consumed_and_returns_only_safe_details(settings, monkeypatch):
    from autobuild_json.tokens import TokenService
    from autobuild_json.errors import FlowError
    from autobuild_json.transport import SafeTransport
    calls = []
    def timeout(self, method, url, **kwargs):
        calls.append((method,url))
        raise FlowError("REQUEST_TIMEOUT", "http", details={"method":"POST", "host":"auth.openai.com",
            "endpoint":"/oauth/token", "curl_code":28, "cookie":"secret-cookie"})
    monkeypatch.setattr(SafeTransport,"request",timeout)
    with TestClient(create_app(settings, token_service=TokenService()), base_url=ORIGIN) as client:
        login = client.post("/api/session", headers={"Origin":ORIGIN}, json={"token":"test-admin-token"})
        client.headers.update({"Origin":ORIGIN,"X-CSRF-Token":login.json()["csrf_token"]})
        session = client.post("/api/oauth/links", json={"email":"u@example.com"}).json()
        authorize = parse_qs(urlsplit(session["desktop_url"]).query)["authorize_url"][0]
        state = parse_qs(urlsplit(authorize).query)["state"][0]
        payload = {"session_id":session["session_id"],"callback_url":f"http://localhost:1455/auth/callback?state={state}&code=fake-code"}
        response = client.post("/api/oauth/complete",json=payload)
        assert response.status_code == 400
        assert response.json()["error"] == "TOKEN_EXCHANGE_UNCERTAIN"
        assert response.json()["details"]["curl_code"] == 28
        assert "secret-cookie" not in response.text
        replay = client.post("/api/oauth/complete",json=payload)
        assert replay.json()["error"] == "INVALID_STATE"
        assert calls == [("POST","https://auth.openai.com/oauth/token")]
