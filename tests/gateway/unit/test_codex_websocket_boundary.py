import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
import pytest
from sqlalchemy.exc import SQLAlchemyError

from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.http import codex_websocket as module
from tests.gateway.websocket_support import asgi_socket

pytestmark = pytest.mark.asyncio
HOST = [(b"host", b"127.0.0.1:8788")]
AUTH = [(b"authorization", b"Bearer synthetic-client")]


def application(*, auth_error=None):
    calls = []

    async def authenticate(secret, protocol):
        calls.append((secret, protocol))
        if auth_error:
            raise auth_error
        if secret != "synthetic-client":
            raise GatewayError("invalid_api_key", 401)
        return object()

    app = FastAPI()
    module.mount_websocket(app, SimpleNamespace(authenticate=authenticate), object(), {"127.0.0.1:8788"})
    return app, calls


@pytest.mark.parametrize("headers,query", [
    (HOST, b""), (AUTH, b""), (HOST+HOST+AUTH, b""),
    ([(b"host", b"evil.invalid")]+AUTH, b""),
    (HOST+AUTH+[(b"origin", b"https://evil.invalid")], b""),
    (HOST+AUTH+[(b"origin", b"http://127.0.0.1:8788")]*2, b""),
    (HOST+AUTH+AUTH, b""), (HOST+AUTH+[(b"x-api-key", b"other")], b""),
    (HOST+AUTH, b"key=do-not-echo"), (HOST+AUTH+[(b"x-gateway-session", b"")], b""),
])
async def test_bad_handshake_is_rejected_before_accept(headers, query):
    app, _ = application()
    async with asgi_socket(app, headers=headers, query=query) as socket:
        assert await socket.receive() == {"type": "websocket.close", "code": 1008, "reason": ""}


async def test_auth_storage_failure_closes_without_http_response_or_details():
    app, _ = application(auth_error=SQLAlchemyError("DO-NOT-ECHO-db-password"))
    async with asgi_socket(app, headers=HOST+AUTH) as socket:
        assert await socket.receive() == {"type": "websocket.close", "code": 1011, "reason": ""}


@pytest.mark.parametrize("stream_id", ["", "bad space", "bad/slash", "é", "x"*257, 5])
async def test_invalid_stream_id_returns_request_error_without_dispatch(stream_id):
    app, calls = application()
    async with asgi_socket(app, headers=HOST+AUTH) as socket:
        await socket.receive()
        result = await socket.turn({"type": "response.create", "stream_id": stream_id})
        assert result[-1]["error"]["code"] == "invalid_request"
        assert "stream_id" not in result[-1]
    assert len(calls) == 1  # Handshake only; invalid turn never gets to engine.


@pytest.mark.parametrize("failure,code,status", [
    (SQLAlchemyError("DO-NOT-ECHO-storage"), "storage_unavailable", 503),
    (RuntimeError("DO-NOT-ECHO-internal"), "internal_error", 500),
])
async def test_turn_failure_is_safe_correlated_and_connection_remains_usable(monkeypatch, failure, code, status):
    app, _ = application()

    async def failing_turn(*args):
        raise failure

    monkeypatch.setattr(module, "serve_turn", failing_turn)
    async with asgi_socket(app, headers=HOST+AUTH) as socket:
        await socket.receive()
        for _ in range(2):
            result = await socket.turn({"type": "response.create", "stream_id": "lane"})
            assert result == [{"type": "error", "status": status, "stream_id": "lane",
                               "error": {"type": "server_error", "code": code, "message": code}}]


async def test_idle_timeout_sends_close_frame(monkeypatch):
    app, _ = application()
    monkeypatch.setattr(module, "IDLE_TIMEOUT", 0.01)
    async with asgi_socket(app, headers=HOST+AUTH) as socket:
        assert (await socket.receive())["type"] == "websocket.accept"
        assert (await socket.receive())["type"] == "websocket.close"


async def test_connection_deadline_cancels_and_joins_active_turn(monkeypatch):
    app, _ = application()
    entered, cleaned = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(module, "CONNECTION_TIMEOUT", 0.05)

    async def blocked_turn(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(module, "serve_turn", blocked_turn)
    async with asgi_socket(app, headers=HOST+AUTH) as socket:
        await socket.receive()
        await socket.send_json({"type": "response.create"})
        await asyncio.wait_for(entered.wait(), 1)
        assert (await socket.receive())["type"] == "websocket.close"
    assert cleaned.is_set()
