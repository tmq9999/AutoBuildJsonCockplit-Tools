import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from tests.gateway.harness import gateway_environment

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def native_wire():
    output = {"id": "msg1", "type": "message", "role": "assistant", "status": "completed",
              "content": [{"type": "output_text", "text": "Native", "annotations": []}]}
    events = [
        {"type": "response.created", "response": {"id": "upstream-resp-private", "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0, "item": dict(output, content=[])},
        {"type": "response.output_text.delta", "item_id": "msg1", "output_index": 0, "content_index": 0, "delta": "Native"},
        {"type": "response.output_item.done", "output_index": 0, "item": output},
        {"type": "response.completed", "response": {"id": "upstream-resp-private", "status": "completed", "output": [output],
            "usage": {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9}}},
    ]
    return b"".join(("event: "+e["type"]+"\ndata: "+json.dumps(e)+"\n\n").encode() for e in events)


async def test_responses_can_translate_a_chat_upstream(pg_db):
    async with gateway_environment(pg_db) as env:
        response = await env.client.post("/v1/responses", json={"model": "public", "input": "Hi", "max_output_tokens": 8},
                                         headers={"Authorization": "Bearer "+env.secret})
        assert response.status_code == 200, response.text
        assert response.json()["output"][0]["content"][0]["text"] == "Hello"
        assert response.json()["usage"]["input_tokens"] == 4


async def test_native_responses_continuation_is_scoped_and_sent_only_upstream(pg_db):
    from autobuild_json.gateway.routing.records import ProviderConfig
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(200, content=native_wire())) as env:
        catalog = env.engine.catalog
        await catalog.update_provider(env.provider_id, 1, ProviderConfig(name="native", adapter="openai_compatible",
            root="https://provider.invalid/v1", wire_api="responses"))
        await catalog.put_binding(replace(env.binding, capabilities=frozenset({"text", "tools", "continuation"})))
        headers = {"Authorization": "Bearer "+env.secret}
        response = await env.client.post("/v1/responses", json={"model": "public", "input": "Hi", "max_output_tokens": 8}, headers=headers)
        assert response.status_code == 200, response.text
        handle = response.json()["id"]
        assert handle.startswith("resp_") and "upstream-resp-private" not in response.text
        next_response = await env.client.post("/v1/responses", json={"model": "public", "input": "Next", "max_output_tokens": 8,
            "previous_response_id": handle}, headers=headers)
        assert next_response.status_code == 200, next_response.text
        assert env.upstream_requests[-1]["body"]["previous_response_id"] == "upstream-resp-private"
        assert env.upstream_requests[-1]["url"].endswith("/responses")


@pytest.mark.parametrize("backup_delay", [0, 10])
@pytest.mark.parametrize("valid_handle", [True, False])
async def test_continuation_keeps_its_binding_cooldown_error(pg_db, backup_delay, valid_handle):
    from datetime import datetime, timedelta, timezone
    from autobuild_json.gateway.routing.continuations import ContinuationBinding, ContinuationScope
    from autobuild_json.gateway.routing.records import ProviderConfig
    async with gateway_environment(pg_db) as env:
        async with pg_db.sessions() as session:
            row = (await session.execute(text("SELECT customer_id,version FROM api_keys WHERE id=:id"), {"id": env.key_id})).one()
        await env.engine.catalog.update_provider(env.provider_id, 1, ProviderConfig(
            name="native", adapter="openai_compatible", root="https://provider.invalid/v1", wire_api="responses"))
        await env.engine.catalog.put_binding(replace(env.binding, capabilities=frozenset({"text", "tools", "continuation"})))
        handle = await env.engine.continuations.issue(
            ContinuationScope(row[0], env.key_id, "public"),
            ContinuationBinding(env.binding.id, env.credential_id, "upstream-response",
                                datetime.now(timezone.utc)+timedelta(minutes=5)))
        backup = await env.engine.catalog.create_provider(ProviderConfig(
            name="Backup", adapter="openai_compatible", root="https://backup.invalid/v1"))
        backup_credential = await env.engine.catalog.put_credential(backup, "synthetic-backup")
        await env.engine.catalog.put_binding(replace(env.binding, id=uuid4(),
            provider_id=backup, credential_id=backup_credential, priority=1,
            capabilities=frozenset({"text", "tools", "continuation"})))
        if backup_delay:
            await env.engine.catalog.cooldown(backup, None, backup_delay)
        await env.engine.catalog.cooldown(env.provider_id, None, 120)
        response = await env.client.post("/v1/responses", json={
            "model": "public", "input": "Next", "max_output_tokens": 8,
            "previous_response_id": handle if valid_handle else "resp_unknown"},
            headers={"Authorization": "Bearer "+env.secret})
        assert response.status_code == (429 if valid_handle else 400)
        assert response.json()["error"]["code"] == ("rate_limited" if valid_handle else "invalid_state")
        if valid_handle:
            assert 118 <= int(response.headers["retry-after"]) <= 120
        assert not env.upstream_requests
