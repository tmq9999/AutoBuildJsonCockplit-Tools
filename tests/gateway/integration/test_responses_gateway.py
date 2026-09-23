import json
from dataclasses import replace

import httpx
import pytest

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
