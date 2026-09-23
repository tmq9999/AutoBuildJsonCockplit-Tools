from fastapi.testclient import TestClient
from autobuild_json.api import create_app

ORIGIN = "http://127.0.0.1:8787"


def test_export_requires_session(client):
    assert client.get("/api/jobs/00000000-0000-4000-8000-000000000001/exports/success").status_code == 401
    assert client.get("/api/health").status_code == 401
    assert client.get("/openapi.json").status_code in {401,404}


def test_validation_errors_never_echo_inputs(authenticated_client):
    client, _ = authenticated_client
    response = client.post("/api/jobs", json={"accounts_text":"u@example.com|private-pass|PRIVATE-SECRET", "proxies_text":"http://u:proxy-password@127.0.0.1:8", "mode":"parallel", "workers":"private-worker-value", "timeout":180})
    assert response.status_code == 422
    for secret in ("private-pass", "PRIVATE-SECRET", "proxy-password", "private-worker-value"):
        assert secret not in response.text
    response = client.post("/api/jobs", json={"private-secret-key":"private-value"})
    assert "private-secret-key" not in response.text


def test_validate_mask_and_mixed_batch_exports(authenticated_client):
    client, _ = authenticated_client
    text = "\n".join(f"{email}|private-pass|JBSWY3DPEHPK3PXP" for email in ["u@example.com", "phone@example.com", "error@example.com"]) + "\ninvalid|private|"
    validated = client.post("/api/accounts/validate", json={"accounts_text":text}).json()
    assert validated["valid_count"] == 3
    assert validated["filtered_count"] == 1
    assert "private-pass" not in str(validated)
    assert validated["preview"][0]["email"] != "u@example.com"
    response = client.post("/api/jobs", json={"accounts_text":text, "mode":"parallel", "workers":3})
    assert response.status_code == 202
    job = response.json()["id"]
    client.app.state.runner.wait(job, 3)
    snapshot = client.get("/api/jobs/"+job).json()
    assert snapshot["status"] == "completed"
    assert snapshot["counts"]["success"] == 1
    assert snapshot["counts"]["phone_verify"] == 1
    assert "refresh_token" not in str(snapshot)
    for kind, count in [("success",1),("errors",1),("phone_verify",1),("filtered",1),("cancelled",0)]:
        exported = client.get(f"/api/jobs/{job}/exports/{kind}")
        assert exported.status_code == 200
        assert len(exported.json()) == count
        assert exported.headers["cache-control"] == "no-store"
        assert "attachment" in exported.headers["content-disposition"]
    assert client.get("/api/jobs").json()[0]["id"] == job


def test_missing_dependency_keeps_validation_and_links_available(settings):
    settings.checklive_path = settings.data_dir / "missing"
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        login = client.post("/api/session", headers={"Origin":ORIGIN}, json={"token":"test-admin-token"})
        client.headers.update({"Origin":ORIGIN,"X-CSRF-Token":login.json()["csrf_token"]})
        assert client.get("/api/health").json()["auth_ready"] is False
        response = client.post("/api/jobs", json={"accounts_text":"u@example.com|p|JBSWY3DPEHPK3PXP"})
        assert response.status_code == 503
        assert str(settings.data_dir) not in response.text
        assert client.post("/api/oauth/links", json={"email":"u@example.com"}).status_code == 201
