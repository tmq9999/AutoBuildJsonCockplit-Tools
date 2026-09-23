import json

import httpx
import pytest

from tests.gateway.harness import gateway_environment

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def enable_anthropic(env):
    from autobuild_json.gateway.identity.service import IdentityService
    from autobuild_json.gateway.identity.policy import KeyPolicy
    await IdentityService(env.engine.db, b"p"*32).update_policy(env.key_id, 1,
        KeyPolicy(model_ids={"public"}, protocols={"openai", "anthropic"}, total_micro=10**12))
    return {"x-api-key": env.secret, "anthropic-version": "2023-06-01"}


async def test_anthropic_client_can_use_chat_provider(pg_db):
    async with gateway_environment(pg_db) as env:
        headers = await enable_anthropic(env)
        response = await env.client.post("/v1/messages", json={"model": "public", "messages": [{"role": "user", "content": "Hi"}],
                                                              "max_tokens": 8}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["content"] == [{"type": "text", "text": "Hello"}]
        assert response.json()["stop_reason"] == "end_turn"
        assert response.json()["usage"]["input_tokens"] == 4
        response = await env.client.post("/v1/messages", json={"model": "public", "messages": [], "max_tokens": 8, "stream": True}, headers=headers)
        assert "event: message_start" in response.text
        assert "event: content_block_delta" in response.text
        assert "event: message_stop" in response.text


async def test_anthropic_native_provider_usage_and_headers(pg_db):
    from autobuild_json.gateway.routing.records import ProviderConfig
    events = [
        {"type": "message_start", "message": {"id": "private", "usage": {"input_tokens": 4, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Anthropic"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
        {"type": "message_stop"}]
    wire = b"".join(("event: "+e["type"]+"\ndata: "+json.dumps(e)+"\n\n").encode() for e in events)
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(200, content=wire)) as env:
        headers = await enable_anthropic(env)
        await env.engine.catalog.update_provider(env.provider_id, 1, ProviderConfig(name="Anthropic", adapter="anthropic",
            root="https://anthropic.invalid/v1", auth_mode="x-api-key"))
        result = await env.client.post("/v1/messages", json={"model": "public", "messages": [], "max_tokens": 8}, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["content"][0]["text"] == "Anthropic"
        assert env.upstream_requests[0]["headers"]["x-api-key"] == "synthetic-upstream-secret"
        assert env.upstream_requests[0]["headers"]["anthropic-version"] == "2023-06-01"


async def test_native_count_tokens_does_not_charge_generation_quota(pg_db):
    from sqlalchemy import text
    from autobuild_json.gateway.routing.records import ProviderConfig
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(200, json={"input_tokens": 17})) as env:
        headers = await enable_anthropic(env)
        await env.engine.catalog.update_provider(env.provider_id, 1, ProviderConfig(name="native", adapter="anthropic",
            root="https://anthropic.invalid/v1", auth_mode="x-api-key"))
        response = await env.client.post("/v1/messages/count_tokens", json={"model": "public", "messages": [{"role": "user", "content": "Hi"}]}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == {"input_tokens": 17}
        assert env.upstream_requests[0]["url"].endswith("/messages/count_tokens")
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT amount_micro FROM usage_ledger")) == 0
