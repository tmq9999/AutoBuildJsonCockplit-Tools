"""Synthetic regressions for shapes observed during bounded live verification."""
import json

import httpx
import pytest
from sqlalchemy import text

from .test_codex_pool_dispatch import BODY, codex_environment
from .test_responses_gateway import native_wire

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def streamed_only_wire():
    frames = []
    for frame in native_wire().split(b"\n\n"):
        if not frame:
            continue
        value = json.loads(frame.split(b"\ndata: ", 1)[1])
        if value["type"] == "response.completed":
            value["response"]["output"] = []
            value["response"]["usage"]["input_tokens_details"] = {"cached_tokens": 3}
        frames.append(("event: " + value["type"] + "\ndata: " + json.dumps(value) + "\n\n").encode())
    return b"".join(frames)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("path", ["/v1/responses", "/v1/chat/completions"])
async def test_empty_terminal_output_serves_streamed_content_and_settles_once(pg_db, path, stream):
    async with codex_environment(pg_db, respond=lambda _: httpx.Response(200, content=streamed_only_wire())) as env:
        body = dict(BODY, stream=stream)
        if path.endswith("completions"):
            body.pop("input")
            body["messages"] = [{"role": "user", "content": "Hi"}]
        response = await env.client.post(path, headers=env.headers, json=body)
        assert response.status_code == 200 and "Native" in response.text
        async with pg_db.sessions() as session:
            assert (await session.execute(text("SELECT state,usage->>'cached_read' FROM requests"))).one() == ("completed", "3")
            assert (await session.execute(text("SELECT count(*),sum(amount_micro) FROM usage_ledger"))).one() == (1, 20)
            assert await session.scalar(text("SELECT held FROM quota_buckets WHERE window_kind='total'")) == 0


@pytest.mark.parametrize("payload,code", [
    ({"detail": "The 'synthetic' model is not supported when using Codex with a ChatGPT account."}, "model_not_supported"),
    ({"detail": "private-provider-value"}, "provider_rejected"),
    ({"detail": {"secret": "private-provider-value"}}, "upstream_error"),
])
async def test_codex_rejection_is_classified_without_leaking_and_retries_only_model_access(pg_db, payload, code):
    async with codex_environment(pg_db, respond=lambda _: httpx.Response(400, json=payload)) as env:
        response = await env.client.post('/v1/responses', json=BODY, headers=env.headers)
        assert response.status_code == 502 and response.json()["error"]["code"] == code
        assert "private-provider-value" not in response.text and "synthetic" not in response.text
        assert len(env.upstream_requests) == (3 if code == 'model_not_supported' else 1)
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT state FROM requests")) == 'released'
            assert await session.scalar(text("SELECT sum(amount_micro) FROM usage_ledger")) == 0
