import threading
import time
import pytest
from autobuild_json.settings import Settings
from autobuild_json.models import AttemptContext


@pytest.fixture(autouse=True)
def deny_outbound_network(monkeypatch):
    import socket
    from curl_cffi import requests
    def denied(*args, **kwargs):
        raise AssertionError("Outbound network is forbidden in offline tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(requests.Session, "request", denied)

@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, installation_id="00000000-0000-4000-8000-000000000001", admin_token="test-admin-token", _env_file=None)

@pytest.fixture
def context():
    return AttemptContext(time.monotonic() + 180, threading.Event(), lambda *args: None)


@pytest.fixture
def client(settings):
    from fastapi.testclient import TestClient
    from autobuild_json.api import create_app
    from autobuild_json.results import RunStore
    from autobuild_json.runner import Runner
    from autobuild_json.models import RunResult
    from autobuild_json.errors import FlowError
    from tests.fakes import success_result
    def processor(account, proxy, context):
        if account.email.startswith("phone"):
            return RunResult("phone_verify", error=FlowError("PHONE_VERIFY"))
        if account.email.startswith("error"):
            return RunResult("error", error=FlowError("INVALID_CREDENTIALS"))
        return success_result(account.email)
    runner = Runner(RunStore(settings.data_dir / "runs"), processor)
    with TestClient(create_app(settings, runner=runner), base_url="http://127.0.0.1:8787") as client:
        yield client


@pytest.fixture
def authenticated_client(client):
    origin = "http://127.0.0.1:8787"
    response = client.post("/api/session", headers={"Origin":origin}, json={"token":"test-admin-token"})
    assert response.status_code == 200
    csrf = response.json()["csrf_token"]
    client.headers.update({"Origin":origin,"X-CSRF-Token":csrf})
    return client, csrf
