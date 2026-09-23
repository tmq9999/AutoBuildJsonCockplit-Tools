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
