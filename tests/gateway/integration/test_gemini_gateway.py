import json
import httpx
import pytest

from tests.gateway.harness import gateway_environment

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def enable_gemini(env):
    from autobuild_json.gateway.identity.policy import KeyPolicy
    from autobuild_json.gateway.identity.service import IdentityService
    await IdentityService(env.engine.db, b"p"*32).update_policy(env.key_id, 1,
        KeyPolicy(model_ids={"public"}, protocols={"gemini", "openai"}, total_micro=10**12))
    return {"x-goog-api-key": env.secret}


async def test_gemini_client_uses_chat_upstream(pg_db):
    async with gateway_environment(pg_db) as env:
        headers = await enable_gemini(env)
        body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}], "generationConfig": {"maxOutputTokens": 8}}
        response = await env.client.post("/v1beta/models/public:generateContent", json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["candidates"][0]["content"]["parts"][0]["text"] == "Hello"
        response = await env.client.post("/v1beta/models/public:streamGenerateContent?alt=sse", json=body, headers=headers)
        assert response.status_code == 200 and "data: " in response.text
        assert "usageMetadata" in response.text


async def test_native_gemini_uses_sse_and_header_key(pg_db):
    from autobuild_json.gateway.routing.records import ProviderConfig
    chunk = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Gemini"}]}, "finishReason": "STOP", "index": 0}],
             "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 5}}
    wire = ("data: "+json.dumps(chunk)+"\n\n").encode()
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(200, content=wire)) as env:
        headers = await enable_gemini(env)
        await env.engine.catalog.update_provider(env.provider_id, 1, ProviderConfig(name="Gemini", adapter="gemini",
            root="https://gemini.invalid/v1beta", auth_mode="x-goog-api-key"))
        response = await env.client.post("/v1beta/models/public:generateContent", json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["candidates"][0]["content"]["parts"][0]["text"] == "Gemini"
        assert env.upstream_requests[0]["url"].endswith(":streamGenerateContent?alt=sse")
        assert env.upstream_requests[0]["headers"]["x-goog-api-key"] == "synthetic-upstream-secret"
