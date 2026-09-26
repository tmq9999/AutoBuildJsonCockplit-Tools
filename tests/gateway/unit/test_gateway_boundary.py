import httpx
import pytest
import asyncio


@pytest.mark.asyncio
async def test_boundary_requires_json_for_mutation_and_limits_unauthenticated_traffic():
    from autobuild_json.gateway.http.boundary import GatewayBoundary
    calls = []
    async def inner(scope, receive, send):
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    app = GatewayBoundary(inner, {"test"}, ingress_rpm=2)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/v1/chat/completions", content="bad")).status_code == 415
        assert (await client.get("/v1/models")).status_code == 200
        assert (await client.get("/v1/models")).status_code == 429
    assert calls == ["/v1/models"]


@pytest.mark.asyncio
async def test_body_size_guard_stops_before_handler():
    from autobuild_json.gateway.http.boundary import GatewayBoundary
    called = False
    async def inner(scope, receive, send):
        nonlocal called
        called = True
    app = GatewayBoundary(inner, {"test"}, max_body=16)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post("/v1/chat/completions", content=b"x"*17, headers={"content-type": "application/json"})
        assert result.status_code == 413
    assert not called


@pytest.mark.asyncio
async def test_boundary_limits_websocket_handshakes_before_authentication():
    from autobuild_json.gateway.http.boundary import GatewayBoundary

    reached = 0

    async def inner(scope, receive, send):
        nonlocal reached
        reached += 1
        await send({"type": "websocket.close", "code": 1000})

    app = GatewayBoundary(inner, {"test"}, ingress_rpm=1)

    async def handshake():
        incoming = asyncio.Queue()
        outgoing = []
        await incoming.put({"type": "websocket.connect"})
        scope = {"type": "websocket", "scheme": "ws", "path": "/v1/responses",
                 "query_string": b"", "headers": [(b"host", b"test")],
                 "client": ("192.0.2.1", 1234)}
        async def send(message):
            outgoing.append(message)
        await app(scope, incoming.get, send)
        return outgoing

    assert (await handshake()) == [{"type": "websocket.close", "code": 1000}]
    assert (await handshake()) == [{"type": "websocket.close", "code": 1013}]
    assert reached == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('websocket_first', [True, False])
async def test_http_and_websocket_share_peer_window_and_recover_after_expiry(monkeypatch, websocket_first):
    from types import SimpleNamespace
    from autobuild_json.gateway.http import boundary

    now = 100.0
    monkeypatch.setattr(boundary, 'time', SimpleNamespace(monotonic=lambda: now))

    async def inner(scope, receive, send):
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.accept'})
        else:
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})

    app = boundary.GatewayBoundary(inner, {'test'}, ingress_rpm=1)

    async def websocket(peer='192.0.2.1'):
        messages = []

        async def send(message):
            messages.append(message)

        async def receive():
            return {'type': 'websocket.connect'}

        await app({'type': 'websocket', 'scheme': 'ws', 'path': '/v1/responses',
                   'headers': [(b'host', b'test')], 'client': (peer, 4321)}, receive, send)
        return messages[0]['type'] == 'websocket.accept'

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('192.0.2.1', 1234)),
                                 base_url='http://test') as client:
        if websocket_first:
            assert await websocket()
            assert (await client.get('/v1/models')).status_code == 429
        else:
            assert (await client.get('/v1/models')).status_code == 200
            assert not await websocket()
        assert await websocket('192.0.2.2')
        now += 60
        assert await websocket()
