from dataclasses import replace
import json

import httpx
import pytest
from sqlalchemy import text

from autobuild_json.gateway.admin.codex_models import PublishCodexModels, publish_codex_models
from autobuild_json.gateway.identity.policy import KeyPolicy
from autobuild_json.gateway.identity.service import IdentityService
from tests.gateway.codex_admin_support import private_env, account
from .test_codex_pool_dispatch import codex_environment

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def image_wire():
    final = {"type": "image_generation_call", "id": "ig_1", "status": "completed",
             "result": "UE5H", "output_format": "png", "revised_prompt": "Blue bird"}
    events = [
        {"type": "response.created", "response": {"id": "resp_image"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "image_generation_call", "id": "ig_1", "status": "in_progress"}},
        {"type": "response.image_generation_call.partial_image", "item_id": "ig_1", "output_index": 0,
         "partial_image_index": 0, "partial_image_b64": "UE5H"},
        {"type": "response.output_item.done", "output_index": 0, "item": final},
        {"type": "response.completed", "response": {"id": "resp_image", "output": [final],
            "usage": {"input_tokens": 4, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 3}}}},
    ]
    return b"".join(("data: "+json.dumps(e)+"\n\n").encode() for e in events)


@pytest.mark.parametrize("path", ["/v1/responses/compact", "/backend-api/codex/responses/compact"])
async def test_compact_uses_native_path_retains_replay_and_bills_cached_usage(pg_db, path):
    output = [{"type": "message", "id": "m", "role": "user", "content": [{"type": "input_text", "text": "Hi"}]},
              {"type": "compaction", "id": "cmp", "encrypted_content": "opaque-replay"}]
    async with codex_environment(pg_db, respond=lambda r: httpx.Response(200, json={
        "object": "response.compaction", "id": "compact_1", "output": output,
        "usage": {"input_tokens": 4, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 3}}})) as env:
        result = await env.client.post(path, json={"model": "codex-test", "input": "Hi"}, headers=env.headers)
        assert result.status_code == 200, result.text
        assert result.json()["object"] == "response.compaction"
        assert result.json()["output"] == output
        sent = env.upstream_requests[0]
        assert sent["url"].endswith("/responses/compact")
        assert set(sent["body"]) == {"model", "input", "instructions"}
        assert sent["headers"]["accept"] == "application/json"
        async with pg_db.sessions() as session:
            assert (await session.execute(text("SELECT count(*),sum(amount_micro) FROM usage_ledger"))).one() == (1, 20)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("edit", [False, True])
async def test_images_generate_and_multipart_edit_use_accounting(pg_db, edit, stream):
    async with codex_environment(pg_db, respond=lambda r: httpx.Response(200, content=image_wire())) as env:
        for binding in env.bindings:
            await env.engine.catalog.put_binding(replace(binding, capabilities=binding.capabilities|{"images", "vision"}))
        if edit:
            result = await env.client.post('/v1/images/edits', headers=env.headers,
                data={"model": "codex-test", "prompt": "paint 50% red", "stream": str(stream).lower()},
                files=[("image[]", ("a.png", b"PNG\r\n-", "image/png")), ("mask", ("b.png", b"mask", "image/png"))])
        else:
            result = await env.client.post('/v1/images/generations', headers=env.headers,
                json={"model": "codex-test", "prompt": "Blue bird", "stream": stream})
        assert result.status_code == 200, result.text
        body = env.upstream_requests[0]["body"]
        assert body["tools"][0]["type"] == "image_generation"
        assert body["tools"][0]["action"] == ("edit" if edit else "generate")
        assert body["tool_choice"] == "required"
        if edit:
            assert body["input"][0]["content"][0]["text"] == "paint 50% red"
            assert body["input"][0]["content"][1]["image_url"] == "data:image/png;base64,UE5HDQot"
            assert body["tools"][0]["input_image_mask"]["image_url"].startswith("data:image/png;base64,")
        if stream:
            assert '.partial_image' in result.text and '.completed' in result.text and 'UE5H' in result.text
        else:
            assert result.json()["data"] == [{"b64_json": "UE5H", "revised_prompt": "Blue bird"}]
            assert result.json()["usage"]["input_tokens_details"]["cached_tokens"] == 3
        async with pg_db.sessions() as session:
            assert (await session.execute(text("SELECT count(*),sum(amount_micro) FROM usage_ledger"))).one() == (1, 20)


async def test_images_not_dispatched_without_account_capability(pg_db):
    async with codex_environment(pg_db) as env:
        result = await env.client.post('/v1/images/generations', headers=env.headers,
            json={"model": "codex-test", "prompt": "Blue bird"})
        assert result.status_code == 400 and not env.upstream_requests
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM requests")) == 0


async def test_publish_creates_routable_models_preserves_rates_and_requires_image_selection(pg_db, settings):
    async with private_env(pg_db, settings) as (client, services):
        credential = await account(services)
        payload = {"model_ids": ["gpt-5.5", "gpt-6-astra"]}
        result = await client.post('/api/service/codex-models/publish', json=payload)
        assert result.status_code == 200, result.text
        assert result.json()["models_created"] == 2 and result.json()["bindings_created"] == 2
        repeat = await client.post('/api/service/codex-models/publish', json=payload)
        assert repeat.json()["models_created"] == repeat.json()["bindings_created"] == 0
        denied = await client.post('/api/service/codex-models/publish', json={"model_ids": ["gpt-image-2.5"]})
        assert denied.status_code == 400
        assert denied.json()["error"]["code"] == "image_accounts_required"
        allowed = await client.post('/api/service/codex-models/publish', json={"model_ids": ["gpt-image-2.5"],
            "image_credential_ids": [str(credential)]})
        assert allowed.status_code == 200, allowed.text


async def test_model_aliases_and_client_catalog_honor_prefix_exclusions(pg_db):
    async with codex_environment(pg_db) as env:
        await publish_codex_models(env.engine, PublishCodexModels(model_ids=["gpt-5.5", "gpt-6-astra"]))
        await IdentityService(pg_db, b'p'*32).update_policy(env.key_id, 2,
            KeyPolicy(all_models=True, excluded_model_ids={"gpt-6-astra", "public", "codex-test"}, model_prefix="team/", protocols={"openai"}))
        for path in ['/models', '/v1/models', '/backend-api/codex/models']:
            result = await env.client.get(path, headers=env.headers)
            assert result.status_code == 200, result.text
            assert [m['id'] for m in result.json()['data']] == ['team/gpt-5.5']
            native = await env.client.get(path+'?client_version=0.1.0', headers=env.headers)
            assert native.status_code == 200, native.text
            assert native.json()['models'][0]['slug'] == 'team/gpt-5.5'
            assert native.json()['models'][0]['context_window'] == 272000
