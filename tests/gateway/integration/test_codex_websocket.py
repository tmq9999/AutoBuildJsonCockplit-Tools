import asyncio
import json

import pytest
from sqlalchemy import text

from autobuild_json.gateway.identity.policy import KeyPolicy
from autobuild_json.gateway.identity.service import IdentityService
from wsproto import ConnectionType, WSConnection
from wsproto.events import AcceptConnection, Request

from tests.gateway.websocket_support import SocketStream, SocketTransport, asgi_socket
from .test_codex_egress import SyntheticNetwork, SyntheticSocket, configure_egress, assert_no_leases
from .test_codex_pool_dispatch import codex_environment
from .test_responses_gateway import native_wire

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
CREATE = {"type": "response.create", "model": "codex-test", "input": "Hi"}


def headers(env):
    return [(b"host", b"127.0.0.1:8788"), (b"authorization", ("Bearer "+env.secret).encode())]


def upstream_events():
    events = [json.loads(line[6:]) for line in native_wire().splitlines() if line.startswith(b"data: ")]
    events[-1]["response"]["usage"]["input_tokens_details"] = {"cached_tokens": 3}
    return events


@pytest.mark.parametrize("path", ["/v1/responses", "/backend-api/codex/responses"])
async def test_websocket_handshake_sequential_turns_and_cached_ledger(pg_db, path):
    async with codex_environment(pg_db) as env:
        adapter = SocketTransport(upstream_events())
        env.engine.transport.adapter = adapter
        async with asgi_socket(env.app, path=path, headers=headers(env)) as socket:
            assert (await socket.receive())["type"] == "websocket.accept"
            first = await socket.turn(dict(CREATE, stream_id="main"))
            assert all(event["stream_id"] == "main" for event in first)
            assert first[-1]["type"] == "response.completed"
            response_id = first[-1]["response"]["id"]
            second = await socket.turn(dict(CREATE, input="Next", previous_response_id=response_id))
            assert second[-1]["type"] == "response.completed"
            assert all("stream_id" not in event for event in second)
        assert len(adapter.requests) == 2
        assert adapter.streams[1].messages[0]["previous_response_id"] == "upstream-resp-private"
        assert all(stream.closed for stream in adapter.streams)
        async with pg_db.sessions() as session:
            assert (await session.execute(text("SELECT count(*),sum(amount_micro) FROM usage_ledger"))).one() == (2, 40)
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0
            assert await session.scalar(text("SELECT sum(held) FROM quota_buckets")) == 0


async def test_named_turn_error_is_correlated_and_next_turn_can_succeed(pg_db):
    async with codex_environment(pg_db) as env:
        adapter = SocketTransport(upstream_events())
        env.engine.transport.adapter = adapter
        async with asgi_socket(env.app, headers=headers(env)) as socket:
            await socket.receive()
            result = await socket.turn(dict(CREATE, stream_id="lane-1", generate=False))
            assert result == [{"type": "error", "status": 400, "stream_id": "lane-1",
                               "error": {"type": "invalid_request_error", "code": "unsupported_feature",
                                         "message": "unsupported_feature"}}]
            assert not adapter.requests
            assert (await socket.turn(dict(CREATE, stream_id="lane-1")))[-1]["type"] == "response.completed"
        assert len(adapter.requests) == 1


async def test_disconnect_cancels_upstream_retains_unknown_usage_and_releases_slot(pg_db):
    async with codex_environment(pg_db) as env:
        adapter = SocketTransport(upstream_events()[:1])
        env.engine.transport.adapter = adapter
        async with asgi_socket(env.app, headers=headers(env)) as socket:
            await socket.receive()
            await socket.send_json(CREATE)
            assert (await socket.receive_json())["type"] == "response.created"
        assert len(adapter.requests) == 1 and adapter.streams[0].closed
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT state FROM requests")) == "usage_pending"
            assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 0
            assert await session.scalar(text("SELECT held FROM quota_buckets WHERE window_kind='total'")) > 0
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0


async def test_key_policy_is_rechecked_for_each_turn(pg_db):
    async with codex_environment(pg_db) as env:
        adapter = SocketTransport(upstream_events())
        env.engine.transport.adapter = adapter
        async with asgi_socket(env.app, headers=headers(env)) as socket:
            await socket.receive()
            assert (await socket.turn(CREATE))[-1]["type"] == "response.completed"
            await IdentityService(pg_db, b"p"*32).update_policy(env.key_id, 2,
                KeyPolicy(model_ids={"different-model"}, protocols={"openai"}))
            failed = (await socket.turn(CREATE))[-1]
            assert failed["type"] == "error"
        assert len(adapter.requests) == 1


async def test_websocket_invalid_upstream_handshake_is_safe_and_has_no_replay(pg_db):
    async with codex_environment(pg_db) as env:
        adapter = SocketTransport(invalid_accept=True)
        env.engine.transport.adapter = adapter
        async with asgi_socket(env.app, headers=headers(env)) as socket:
            await socket.receive()
            result = await socket.turn(dict(CREATE, stream_id="main"))
            assert result[-1]["type"] == "error" and result[-1]["stream_id"] == "main"
            assert result[-1]["error"]["code"] == "upstream_error"
        assert len(adapter.requests) == 1 and adapter.responses[0].is_closed


async def test_websocket_disconnect_during_prepare_joins_cleanup(pg_db, monkeypatch):
    async with codex_environment(pg_db) as env:
        adapter = SocketTransport()
        env.engine.transport.adapter = adapter
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def blocked_prepare(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        monkeypatch.setattr(env.engine, "prepare", blocked_prepare)
        async with asgi_socket(env.app, headers=headers(env)) as socket:
            await socket.receive()
            await socket.send_json(CREATE)
            await asyncio.wait_for(entered.wait(), 5)
        assert cleaned.is_set() and not adapter.requests


class UpgradeSocket(SyntheticSocket):
    """Real httpcore CONNECT/TLS/HTTP upgrade, with only network I/O synthetic."""
    def __init__(self, owner, port):
        super().__init__(owner, port)
        self.peer = SocketStream(WSConnection(ConnectionType.SERVER), upstream_events())
        self.handshake = None
        self.upgraded = False

    async def read(self, max_bytes, timeout=None):
        if not self.connect_returned:
            self.connect_returned = True
            return b"HTTP/1.1 200 Connection established\r\n\r\n"
        if not self.upgraded:
            assert self.handshake
            self.upgraded = True
            return self.handshake
        return await self.peer.read(max_bytes, timeout)

    async def write(self, buffer, timeout=None):
        if self.upgraded:
            return await self.peer.write(buffer, timeout)
        await super().write(buffer, timeout)
        if buffer.startswith(b"GET "):
            self.peer.protocol.receive_data(buffer)
            assert any(isinstance(event, Request) for event in self.peer.protocol.events())
            self.handshake = self.peer.protocol.send(AcceptConnection())


class UpgradeNetwork(SyntheticNetwork):
    async def connect_tcp(self, host, port, **kwargs):
        self.connections.append((host, port))
        socket = UpgradeSocket(self, port)
        self.sockets.append(socket)
        return socket


@pytest.mark.parametrize("mode,port", [("direct", 443), ("fixed", 8101), ("pool", 8102), ("kiotproxy", 8104)])
async def test_websocket_real_httpcore_upgrade_obeys_proxy_and_settles_cached_usage(pg_db, monkeypatch, mode, port):
    from autobuild_json.gateway.transport import network
    backend = UpgradeNetwork()
    monkeypatch.setattr(network, "AutoBackend", lambda: backend)
    async with codex_environment(pg_db) as env:
        calls, _ = await configure_egress(pg_db, env, mode)
        async with asgi_socket(env.app, headers=headers(env)) as socket:
            await socket.receive()
            result = await socket.turn(CREATE)
            assert result[-1]["type"] == "response.completed", result[-1]
        assert backend.connections == [("93.184.216.34", port)]
        upstream = backend.sockets[0]
        wire = b"".join(upstream.writes)
        assert (b"CONNECT chatgpt.com:443 HTTP/1.1" in wire) == (mode != "direct")
        assert b"GET /backend-api/codex/responses HTTP/1.1" in wire
        assert b"upgrade: websocket" in wire.lower()
        assert upstream.tls_names == ["chatgpt.com"] and upstream.closed
        assert upstream.peer.messages[0]["type"] == "response.create"
        assert len(calls) == (1 if mode == "kiotproxy" else 0)
        async with pg_db.sessions() as session:
            assert (await session.execute(text("SELECT count(*),sum(amount_micro) FROM usage_ledger"))).one() == (1, 20)
        await assert_no_leases(pg_db)
