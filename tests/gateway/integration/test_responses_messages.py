"""Offline Responses envelope regressions: real gateway/DB, synthetic provider I/O."""
import json

import pytest
from sqlalchemy import text

from tests.gateway.integration.test_codex_pool_dispatch import codex_environment

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.mark.parametrize("path", ["/v1/responses", "/backend-api/codex/responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("field", ["input", "messages"])
async def test_responses_text_envelopes_dispatch_and_settle_once(pg_db, path, stream, field):
    async with codex_environment(pg_db, count=1) as env:
        await env.engine.catalog.put_alias("gpt-6-astra", "codex-test")
        body = {"model": "gpt-6-astra", field: [{"role": "user", "content": "Xin chào"}]}
        if stream:
            body["stream"] = True
        result = await env.client.post(path, json=body, headers=env.headers)

        assert result.status_code == 200
        if stream:
            assert result.headers["content-type"].startswith("text/event-stream")
            events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
            assert any(event["type"] == "response.output_text.delta" and event["delta"] == "Native"
                       for event in events)
            completed = [event for event in events if event["type"] == "response.completed"]
            assert len(completed) == 1
            response = completed[0]["response"]
        else:
            response = result.json()
        assert response["object"] == "response"
        assert response["status"] == "completed"
        assert response["output"][0]["content"][0]["text"] == "Native"
        assert response["usage"]["input_tokens"] == 4
        assert response["usage"]["output_tokens"] == 5
        assert "upstream-resp-private" not in result.text
        assert len(env.upstream_requests) == 1
        outbound = env.upstream_requests[0]
        assert outbound["url"].endswith("/responses")
        assert outbound["body"]["input"] == [
            {"role": "user", "content": [{"type": "input_text", "text": "Xin chào"}]},
        ]
        assert "messages" not in outbound["body"]
        async with pg_db.sessions() as session:
            assert (await session.execute(text("SELECT count(*),sum(amount_micro) FROM usage_ledger"))).one() == (1, 23)
            assert await session.scalar(text("SELECT state FROM requests")) == "completed"
            assert await session.scalar(text("SELECT count(*) FROM attempts")) == 1
            assert await session.scalar(text("SELECT held FROM quota_buckets WHERE window_kind='total'")) == 0


@pytest.mark.parametrize(("extra", "code"), [
    ({"input": "Also provided"}, "invalid_request"),
    ({"messages": {}}, "invalid_request"),
    ({"messages": ""}, "invalid_request"),
    ({"messages": None}, "invalid_request"),
    ({"messages": [{"role": "user", "content": [{"type": "input_audio", "data": "not-supported"}]}]},
     "unsupported_feature"),
    ({"background": True}, "unsupported_feature"),
    ({"store": True}, "unsupported_feature"),
    ({"max_tokens": 8}, "unsupported_feature"),
])
async def test_invalid_responses_messages_fail_before_dispatch_or_hold(pg_db, extra, code):
    async with codex_environment(pg_db, count=1) as env:
        body = {"model": "codex-test", "messages": [{"role": "user", "content": "DO-NOT-ECHO"}], **extra}
        result = await env.client.post("/v1/responses", json=body, headers=env.headers)

        assert result.status_code == 400
        assert result.json()["error"] == {"code": code, "message": code, "stage": "request"}
        assert not env.upstream_requests
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM requests")) == 0
            assert await session.scalar(text("SELECT count(*) FROM attempts")) == 0
            assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 0
