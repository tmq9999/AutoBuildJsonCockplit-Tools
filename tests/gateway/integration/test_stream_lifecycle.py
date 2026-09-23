from dataclasses import replace
from datetime import datetime, timezone, timedelta
from uuid import uuid4

import httpx
import pytest

from tests.gateway.harness import gateway_environment, chat_success
from .test_gateway import BODY

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_uncertain_timeout_is_not_retried_and_keeps_hold(pg_db):
    from sqlalchemy import text
    def respond(request):
        raise httpx.ReadTimeout("upstream-secret-in-exception", request=request)
    async with gateway_environment(pg_db, respond=respond) as env:
        response = await env.client.post("/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer "+env.secret})
        assert response.status_code == 504
        assert "upstream-secret" not in response.text and len(env.upstream_requests) == 1
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT state FROM requests")) == "usage_pending"


async def test_safe_retry_uses_other_provider_and_charges_once(pg_db):
    from sqlalchemy import text
    from autobuild_json.gateway.routing.records import ProviderConfig
    count = 0
    def respond(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(429, headers={"retry-after": "30"})
        return httpx.Response(200, content=chat_success())
    async with gateway_environment(pg_db, respond=respond) as env:
        catalog = env.engine.catalog
        provider = await catalog.create_provider(ProviderConfig(name="backup", adapter="openai_compatible", root="https://backup.invalid/v1"))
        credential = await catalog.put_credential(provider, "backup-secret")
        await catalog.put_binding(replace(env.binding, id=uuid4(), provider_id=provider, credential_id=credential, priority=1))
        response = await env.client.post("/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer "+env.secret})
        assert response.status_code == 200, response.text
        assert len(env.upstream_requests) == 2
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 1
            assert await session.scalar(text("SELECT count(*) FROM attempts")) == 2


async def test_settlement_failure_never_sends_success_terminal(pg_db, monkeypatch):
    async with gateway_environment(pg_db) as env:
        async def fail(*args):
            raise OSError("disk-full-with-secret")
        monkeypatch.setattr(env.ledger, "settle", fail)
        response = await env.client.post("/v1/chat/completions", json=dict(BODY, stream=True),
                                         headers={"Authorization": "Bearer "+env.secret})
        assert "[DONE]" not in response.text
        assert "storage_unavailable" in response.text and "disk-full" not in response.text


async def test_closing_midstream_releases_upstream_but_not_quota(pg_db):
    from sqlalchemy import text
    from autobuild_json.gateway.identity.policy import Principal
    from autobuild_json.gateway.protocols.openai_chat import OpenAIChatCodec
    from autobuild_json.gateway.engine import RequestMeta
    async with gateway_environment(pg_db) as env:
        async with pg_db.sessions() as session:
            row = (await session.execute(text("SELECT customer_id,version FROM api_keys WHERE id=:id"), {"id": env.key_id})).one()
        principal = Principal(row[0], env.key_id, row[1])
        now = datetime.now(timezone.utc)
        request = OpenAIChatCodec().decode(BODY, {})
        prepared = await env.engine.prepare(principal, request, RequestMeta(uuid4(), now, now+timedelta(seconds=60)))
        iterator = prepared.events()
        assert (await iterator.__anext__()).kind == "started"
        await iterator.aclose()
        assert prepared.stream.response.raw.is_closed
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT state FROM requests")) == "usage_pending"


async def test_db_failure_on_cleanup_still_closes_provider(pg_db, monkeypatch):
    from sqlalchemy import text
    from autobuild_json.gateway.identity.policy import Principal
    from autobuild_json.gateway.protocols.openai_chat import OpenAIChatCodec
    async with gateway_environment(pg_db) as env:
        async with pg_db.sessions() as session:
            row = (await session.execute(text("SELECT customer_id,version FROM api_keys WHERE id=:id"), {"id": env.key_id})).one()
        prepared = await env.engine.prepare(Principal(row[0], env.key_id, row[1]), OpenAIChatCodec().decode(BODY, {}), env.engine.meta(BODY))
        async def fail(*args):
            raise OSError("db-unavailable")
        monkeypatch.setattr(env.ledger, "mark_pending", fail)
        with pytest.raises(OSError):
            await prepared.close()
        assert prepared.stream.response.raw.is_closed


async def test_asgi_disconnect_cancels_real_prepared_call(pg_db):
    import asyncio
    from sqlalchemy import text
    from autobuild_json.gateway.identity.policy import Principal
    from autobuild_json.gateway.protocols.openai_chat import OpenAIChatCodec
    from autobuild_json.gateway.http.streaming import GatewayStreamResponse
    async with gateway_environment(pg_db) as env:
        async with pg_db.sessions() as session:
            row = (await session.execute(text("SELECT customer_id,version FROM api_keys WHERE id=:id"), {"id": env.key_id})).one()
        codec = OpenAIChatCodec()
        prepared = await env.engine.prepare(Principal(row[0], env.key_id, row[1]), codec.decode(BODY, {}), env.engine.meta(BODY))
        gate = asyncio.Event()
        output = []
        async def send(message):
            output.append(message)
            if message["type"] == "http.response.body":
                gate.set()
                await asyncio.sleep(0.1)
        async def receive():
            await gate.wait()
            return {"type": "http.disconnect"}
        await GatewayStreamResponse(prepared, codec)({"type": "http"}, receive, send)
        assert prepared.closed and prepared.stream.response.raw.is_closed
        assert not any(b"[DONE]" in item.get("body", b"") for item in output)


async def test_concurrent_close_waits_for_accounting_and_lease_cleanup(pg_db, monkeypatch):
    import asyncio
    from sqlalchemy import text
    from autobuild_json.gateway.identity.policy import Principal
    from autobuild_json.gateway.protocols.openai_chat import OpenAIChatCodec

    async with gateway_environment(pg_db) as env:
        async with pg_db.sessions() as session:
            row = (await session.execute(text("SELECT customer_id,version FROM api_keys WHERE id=:id"),
                                         {"id": env.key_id})).one()
        prepared = await env.engine.prepare(Principal(row[0], env.key_id, row[1]),
                                            OpenAIChatCodec().decode(BODY, {}), env.engine.meta(BODY))
        entered, release = asyncio.Event(), asyncio.Event()
        original = env.ledger.mark_pending
        calls = []

        async def delayed(*args):
            calls.append(args[0])
            entered.set()
            await release.wait()
            await original(*args)

        monkeypatch.setattr(env.ledger, "mark_pending", delayed)
        first = asyncio.create_task(prepared.close())
        await entered.wait()
        second = asyncio.create_task(prepared.close())
        await asyncio.sleep(0)
        returned_early = second.done()
        closed_early = prepared.closed
        release.set()
        await asyncio.gather(first, second)
        assert not returned_early and not closed_early
        assert prepared.closed and prepared.stream.response.raw.is_closed
        assert len(calls) == 1
