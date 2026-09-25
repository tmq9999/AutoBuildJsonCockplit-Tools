import json

import httpx
import pytest
from sqlalchemy import text

from tests.gateway.unit.test_responses_reasoning import reasoning_events, reasoning_wire
from .test_codex_pool_dispatch import codex_environment

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.mark.parametrize("stream", [False, True])
async def test_codex_native_reasoning_roundtrip_accounts_exactly_once(pg_db, stream):
    async with codex_environment(pg_db, respond=lambda request: httpx.Response(200, content=reasoning_wire())) as env:
        response = await env.client.post("/v1/responses", headers=env.headers,
            json={"model": "codex-test", "input": "Hi", "stream": stream})
        assert response.status_code == 200, response.text
        if stream:
            records = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            completed = records[-1]["response"]
            assert any(item["type"] == "response.reasoning_summary_text.delta" for item in records)
        else:
            completed = response.json()
        reasoning = completed["output"][0]
        assert reasoning["type"] == "reasoning"
        assert reasoning["summary"][0]["text"] == "Published summary."
        assert reasoning["encrypted_content"] == "opaque-replay-payload"
        assert completed["output"][1]["content"][0]["text"] == "Answer"
        assert completed["usage"]["output_tokens_details"]["reasoning_tokens"] == 2
        async with pg_db.sessions() as session:
            assert (await session.execute(text("SELECT count(*),sum(amount_micro) FROM usage_ledger"))).one() == (1, 20)
            assert await session.scalar(text("SELECT state FROM requests")) == "completed"
            assert await session.scalar(text("SELECT held FROM quota_buckets WHERE window_kind='total'")) == 0
        replay = await env.client.post("/v1/responses", headers=env.headers,
            json={"model": "codex-test", "input": [reasoning, {"role": "user", "content": "Next"}]})
        assert replay.status_code == 200, replay.text
        assert env.upstream_requests[-1]["body"]["input"][0] == reasoning


@pytest.mark.parametrize("protocol", ["chat", "anthropic", "gemini", "ollama"])
@pytest.mark.parametrize("stream", [False, True])
async def test_codex_other_protocols_do_not_expose_native_reasoning(pg_db, protocol, stream):
    async with codex_environment(pg_db, respond=lambda request: httpx.Response(200, content=reasoning_wire())) as env:
        path, body = {
            "chat": ("/v1/chat/completions", {"model": "codex-test", "messages": [{"role": "user", "content": "Hi"}], "stream": stream}),
            "anthropic": ("/v1/messages", {"model": "codex-test", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 10, "stream": stream}),
            "gemini": ("/v1beta/models/codex-test:"+("streamGenerateContent?alt=sse" if stream else "generateContent"),
                       {"contents": [{"role": "user", "parts": [{"text": "Hi"}]}]}),
            "ollama": ("/api/chat", {"model": "codex-test", "messages": [{"role": "user", "content": "Hi"}], "stream": stream}),
        }[protocol]
        headers = dict(env.headers)
        if protocol == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        response = await env.client.post(path, headers=headers, json=body)
        assert response.status_code == 200, response.text
        assert "Answer" in response.text
        assert "opaque-replay-payload" not in response.text and "Published summary." not in response.text
        if protocol == "anthropic" and stream:
            records = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            assert [item["index"] for item in records if item["type"] == "content_block_start"] == [0]
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT sum(amount_micro) FROM usage_ledger")) == 20


async def test_malformed_reasoning_never_settles_or_retries_unknown_usage(pg_db):
    events = reasoning_events()
    del events[10]  # Missing reasoning item completion.
    async with codex_environment(pg_db, respond=lambda request: httpx.Response(200, content=reasoning_wire(events))) as env:
        response = await env.client.post("/v1/responses", headers=env.headers, json={"model": "codex-test", "input": "Hi"})
        assert response.status_code == 502
        assert len(env.upstream_requests) == 1
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT state FROM requests")) == "usage_pending"
            assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 0
            assert await session.scalar(text("SELECT held FROM quota_buckets WHERE window_kind='total'")) == 5500
