import asyncio
import socket

import pytest_asyncio
import uvicorn

from tests.gateway.harness import gateway_environment

RAW_CONNECT = socket.socket.connect


@pytest_asyncio.fixture
async def sdk_server(pg_db, monkeypatch, unused_tcp_port):
    from autobuild_json.gateway.identity.policy import KeyPolicy
    from autobuild_json.gateway.identity.service import IdentityService
    async with gateway_environment(pg_db) as env:
        await IdentityService(pg_db, b"p"*32).update_policy(env.key_id, 1,
            KeyPolicy(model_ids={"public"}, protocols={"openai", "anthropic", "gemini", "ollama"}, total_micro=10**12, concurrency=10))
        # Guard only this fixture's listener port; all other Python socket connects
        # remain forbidden. libpq still uses the separate validated test database.
        def connect(sock, address):
            if isinstance(address, tuple) and address[:2] == ("127.0.0.1", unused_tcp_port):
                return RAW_CONNECT(sock, address)
            raise AssertionError("SDK attempted unexpected outbound network")
        monkeypatch.setattr(socket.socket, "connect", connect)
        server = uvicorn.Server(uvicorn.Config(env.app, host="127.0.0.1", port=unused_tcp_port, access_log=False,
                                               log_level="error", lifespan="off"))
        # The app Host allowlist is explicit; this ephemeral host is test-only.
        env.app.user_middleware[0].kwargs["allowed_hosts"] = {f"127.0.0.1:{unused_tcp_port}"}
        task = asyncio.create_task(server.serve())
        try:
            for _ in range(100):
                if server.started:
                    break
                if task.done():
                    await task
                await asyncio.sleep(0.01)
            assert server.started
            env.base_url = f"http://127.0.0.1:{unused_tcp_port}"
            yield env
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)
