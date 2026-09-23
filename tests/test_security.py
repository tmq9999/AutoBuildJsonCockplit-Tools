import pytest
from tests.test_api import ORIGIN


def test_login_cookie_csrf_logout(client):
    response = client.post("/api/session", headers={"Origin":ORIGIN}, json={"token":"test-admin-token"})
    assert response.status_code == 200
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()
    assert client.post("/api/accounts/validate", headers={"Origin":ORIGIN}, json={"accounts_text":""}).status_code == 403
    csrf = response.json()["csrf_token"]
    assert client.delete("/api/session", headers={"Origin":ORIGIN,"X-CSRF-Token":csrf}).status_code == 204
    assert client.get("/api/health").status_code == 401


@pytest.mark.parametrize("headers", [{"Origin":"https://evil.test"}, {"Host":"evil.test","Origin":ORIGIN}, {"Origin":"null"}, {}])
def test_foreign_origin_or_host_rejected(client, headers):
    response = client.post("/api/session", headers=headers, json={"token":"test-admin-token"})
    assert response.status_code in {400,403}


def test_bad_json_and_query_redacted(authenticated_client):
    client, _ = authenticated_client
    response = client.post("/api/jobs", content=b'{"password":"private-pass",oops}', headers={"Content-Type":"application/json"})
    assert response.status_code == 422
    assert "private-pass" not in response.text
    assert client.get("/api/health?token=secret").status_code == 400


def test_chunked_body_bounded(authenticated_client):
    client, _ = authenticated_client
    response = client.post("/api/accounts/validate", content=(b"x" * 1024 * 1024 for _ in range(33)), headers={"Content-Type":"application/json"})
    assert response.status_code == 413


def test_wrong_login_is_safe_and_rate_limited(client):
    for _ in range(5):
        response = client.post("/api/session", headers={"Origin":ORIGIN}, json={"token":"private-bad-token"})
        assert response.status_code == 401
        assert "private-bad-token" not in response.text
    assert client.post("/api/session", headers={"Origin":ORIGIN}, json={"token":"test-admin-token"}).status_code == 429


def test_duplicate_application_process_rejected(settings):
    from fastapi.testclient import TestClient
    from autobuild_json.api import create_app
    from autobuild_json.errors import FlowError
    with TestClient(create_app(settings), base_url=ORIGIN):
        with pytest.raises(FlowError) as error:
            with TestClient(create_app(settings), base_url=ORIGIN):
                pass
        assert error.value.code == "CONFIGURATION_ERROR"
    # Closing the first application releases the lock.
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        assert client.get("/").status_code == 200
