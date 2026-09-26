import asyncio
import time
from types import SimpleNamespace

import pytest
from wsproto.events import Ping, Pong, TextMessage

from autobuild_json.gateway.errors import GatewayError, UpstreamRejected
from autobuild_json.gateway.transport.egress import EgressPolicy
from autobuild_json.gateway.transport.http import Transport
from autobuild_json.gateway.transport.websocket import open_codex_websocket
from tests.gateway.websocket_support import SocketTransport

pytestmark = pytest.mark.asyncio
ROUTE = SimpleNamespace(root="https://chatgpt.com/backend-api/codex")


async def public_address(host, port):
    return ["93.184.216.34"]


def connect(adapter):
    return open_codex_websocket(Transport(EgressPolicy(resolver=public_address), adapter=adapter),
                               ROUTE, None, "synthetic-account", "synthetic-token", time.monotonic()+10)


async def test_real_handshake_fragmented_text_ping_and_coalesced_frames():
    adapter = SocketTransport()
    async with connect(adapter) as socket:
        await socket.send_json({"type": "response.create", "input": "hello"})
        stream = adapter.streams[0]
        assert stream.messages == [{"type": "response.create", "input": "hello"}]
        wire = b"".join(stream.protocol.send(event) for event in [
            TextMessage(data='{"type":"first",', message_finished=False), Ping(payload=b"ping"),
            TextMessage(data='"value":"hello"}'), TextMessage(data='{"type":"second"}')])
        await stream.incoming.put(wire)
        assert await socket.receive_json() == {"type": "first", "value": "hello"}
        assert await socket.receive_json() == {"type": "second"}
        assert any(isinstance(event, Pong) and event.payload == b"ping" for event in stream.received)
    request = adapter.requests[0]
    assert str(request.url) == ROUTE.root+"/responses"
    assert request.headers["authorization"] == "Bearer synthetic-token"
    assert request.headers["chatgpt-account-id"] == "synthetic-account"
    assert request.headers["openai-beta"] == "responses_websockets=2026-02-06"
    assert stream.closed and adapter.responses[0].is_closed


async def test_invalid_handshake_is_safe_gateway_error_and_closes_response():
    adapter = SocketTransport(invalid_accept=True)
    with pytest.raises(GatewayError, match="^upstream_error$"):
        async with connect(adapter):
            pytest.fail("invalid handshake accepted")
    assert adapter.responses[0].is_closed


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_handshake_rejection_preserves_retry_classification(status):
    adapter = SocketTransport(status=status)
    with pytest.raises(UpstreamRejected) as caught:
        async with connect(adapter):
            pytest.fail("rejected handshake accepted")
    assert caught.value.upstream_status == status
    assert caught.value.retry_after == (10 if status == 429 else None)
    assert adapter.responses[0].is_closed


async def test_proxy_transport_is_selected_once_without_direct_fallback(monkeypatch):
    from autobuild_json.gateway.transport import websocket as module
    proxy = SimpleNamespace(server="http://proxy.invalid:8080")
    adapter = SocketTransport(status=503)
    selected = []

    def transport_factory(policy, endpoint):
        selected.append(endpoint)
        return adapter

    monkeypatch.setattr(module, "CoreTransport", transport_factory)
    policy = EgressPolicy(resolver=public_address, trusted_proxy_origins={proxy.server})
    with pytest.raises(GatewayError, match="^upstream_error$"):
        async with open_codex_websocket(Transport(policy), ROUTE, proxy,
                "synthetic-account", "synthetic-token", time.monotonic()+10):
            pytest.fail("failed upstream accepted")
    assert selected == [proxy] and len(adapter.requests) == 1 and adapter.closed


async def test_private_destination_is_rejected_before_handshake():
    async def private_address(host, port):
        return ["127.0.0.1"]

    adapter = SocketTransport()
    with pytest.raises(GatewayError, match="^egress_denied$"):
        async with open_codex_websocket(Transport(EgressPolicy(resolver=private_address), adapter=adapter),
                ROUTE, None, "synthetic-account", "synthetic-token", time.monotonic()+10):
            pytest.fail("private egress accepted")
    assert not adapter.requests


async def test_close_waits_for_inflight_cleanup_after_repeated_cancellation():
    adapter = SocketTransport()
    async with connect(adapter) as socket:
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def slow_close():
            calls.append(1)
            entered.set()
            await release.wait()

        adapter.streams[0].aclose = slow_close
        first = asyncio.create_task(socket.cancel())
        await entered.wait()
        first.cancel()
        await asyncio.sleep(0)
        second = asyncio.create_task(socket.cancel())
        await asyncio.sleep(0)
        first.cancel()
        returned_early = second.done()
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        assert not returned_early
        assert calls == [1]
