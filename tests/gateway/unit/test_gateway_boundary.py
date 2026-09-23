import httpx
import pytest


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
