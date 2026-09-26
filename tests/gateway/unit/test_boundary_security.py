import httpx
import pytest


@pytest.mark.asyncio
async def test_admin_boundary_rejects_duplicate_origin_before_handler():
    from autobuild_json.security import BoundaryMiddleware

    called = False

    async def inner(scope, receive, send):
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = BoundaryMiddleware(inner, 8787)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787"
    ) as client:
        response = await client.post(
            "/api/session",
            content=b"{}",
            headers=[
                ("content-type", "application/json"),
                ("origin", "https://attacker.invalid"),
                ("origin", "http://127.0.0.1:8787"),
            ],
        )

    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert not called
