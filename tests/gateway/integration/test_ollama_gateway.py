import json
import httpx
import pytest

from tests.gateway.harness import gateway_environment

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def enable_ollama(env):
    from autobuild_json.gateway.identity.service import IdentityService
    from autobuild_json.gateway.identity.policy import KeyPolicy
    await IdentityService(env.engine.db, b"p"*32).update_policy(env.key_id, 1,
        KeyPolicy(model_ids={"public"}, protocols={"ollama", "openai"}, total_micro=10**12))
    return {"Authorization": "Bearer "+env.secret}


async def test_ollama_chat_generate_and_discovery_are_authenticated(pg_db):
    async with gateway_environment(pg_db) as env:
        headers = await enable_ollama(env)
        assert (await env.client.get("/api/tags")).status_code == 401
        response = await env.client.post("/api/generate", json={"model": "public", "prompt": "hi", "stream": False}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["response"] == "Hello" and response.json()["done"] is True
        response = await env.client.post("/api/chat", json={"model": "public", "messages": [{"role": "user", "content": "hi"}]}, headers=headers)
        records = [json.loads(line) for line in response.text.splitlines()]
        assert records[-1]["done"] is True and records[-1]["prompt_eval_count"] == 4
        assert response.headers["content-type"].startswith("application/x-ndjson")
        models = await env.client.get("/api/tags", headers=headers)
        assert [m["name"] for m in models.json()["models"]] == ["public"]


async def test_native_ollama_ndjson_and_token_counts(pg_db):
    from autobuild_json.gateway.routing.records import ProviderConfig
    wire = b'{"model":"native","message":{"role":"assistant","content":"Ollama"},"done":false}\n{"message":{"role":"assistant","content":""},"done":true,"done_reason":"stop","prompt_eval_count":4,"eval_count":5}\n'
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(200, content=wire)) as env:
        headers = await enable_ollama(env)
        await env.engine.catalog.update_provider(env.provider_id, 1, ProviderConfig(name="Ollama", adapter="ollama",
            root="https://ollama.invalid/api", auth_mode="bearer"))
        response = await env.client.post("/api/chat", json={"model": "public", "messages": [], "stream": False}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["message"]["content"] == "Ollama"
        assert response.json()["eval_count"] == 5
