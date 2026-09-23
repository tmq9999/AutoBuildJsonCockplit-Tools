import json

import httpx
import pytest
from sqlalchemy import text

from tests.gateway.harness import gateway_environment
from autobuild_json.gateway.identity.policy import KeyPolicy
from autobuild_json.gateway.identity.service import IdentityService
from autobuild_json.gateway.routing.records import ProviderConfig

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def wire(adapter):
    if adapter == "openai_compatible":
        rows = [
            {"choices": [{"index": 0, "delta": {"content": "bridge-test"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 5}},
        ]
        return b"".join(("data: " + json.dumps(r) + "\n\n").encode() for r in rows) + b"data: [DONE]\n\n"
    if adapter == "anthropic":
        rows = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 4, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "bridge-test"},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
        return b"".join(("event: " + r["type"] + "\ndata: " + json.dumps(r) + "\n\n").encode() for r in rows)
    if adapter == "gemini":
        return (
            "data: "
            + json.dumps(
                {
                    "candidates": [{"content": {"parts": [{"text": "bridge-test"}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 5},
                }
            )
            + "\n\n"
        ).encode()
    return b'{"message":{"role":"assistant","content":"bridge-test"},"done":false}\n{"message":{"role":"assistant","content":""},"done":true,"prompt_eval_count":4,"eval_count":5,"done_reason":"stop"}\n'


@pytest.mark.parametrize("adapter", ["openai_compatible", "anthropic", "gemini", "ollama"])
@pytest.mark.parametrize("client", ["openai", "responses", "anthropic", "gemini", "ollama"])
@pytest.mark.parametrize("stream", [False, True])
async def test_cross_protocol_text_and_accounting(pg_db, adapter, client, stream):
    async with gateway_environment(
        pg_db, respond=lambda r: httpx.Response(200, content=wire(adapter))
    ) as env:
        protocol = "openai" if client == "responses" else client
        await IdentityService(pg_db, b"p" * 32).update_policy(
            env.key_id, 1, KeyPolicy(model_ids={"public"}, protocols={protocol}, total_micro=10**12)
        )
        await env.engine.catalog.update_provider(
            env.provider_id,
            1,
            ProviderConfig(
                name="test",
                adapter=adapter,
                root="https://provider.invalid/v1",
                auth_mode={"anthropic": "x-api-key", "gemini": "x-goog-api-key"}.get(adapter, "bearer"),
            ),
        )
        headers = {"Authorization": "Bearer " + env.secret}
        if client == "responses":
            path = "/v1/responses"
            body = {"model": "public", "input": "hello", "stream": stream, "max_output_tokens": 8}
        elif client == "anthropic":
            path = "/v1/messages"
            headers["anthropic-version"] = "2023-06-01"
            body = {
                "model": "public",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": stream,
                "max_tokens": 8,
            }
        elif client == "gemini":
            path = "/v1beta/models/public:" + (
                "streamGenerateContent?alt=sse" if stream else "generateContent"
            )
            body = {
                "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
                "generationConfig": {"maxOutputTokens": 8},
            }
        else:
            path = "/api/chat" if client == "ollama" else "/v1/chat/completions"
            body = {"model": "public", "messages": [{"role": "user", "content": "hello"}], "stream": stream}
            body.update({"options": {"num_predict": 8}} if client == "ollama" else {"max_tokens": 8})
        response = await env.client.post(path, json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert "bridge-test" in response.text
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 1
            assert await session.scalar(text("SELECT amount_micro FROM usage_ledger")) == 9_000_000
