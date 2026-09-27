import httpx
import pytest

from tests.gateway.harness import gateway_environment, chat_success

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
BODY = {"model": "public", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 8}


async def test_public_gateway_has_no_admin_and_accounts_real_usage(pg_db):
    from sqlalchemy import text
    async with gateway_environment(pg_db) as env:
        result = await env.client.post("/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer "+env.secret})
        assert result.status_code == 200, result.text
        assert result.json()["usage"]["prompt_tokens"] == 4
        assert result.json()["choices"][0]["message"]["content"] == "Hello"
        assert (await env.client.get("/api/jobs")).status_code == 404
        assert env.secret not in str(env.upstream_requests)
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT amount_micro FROM usage_ledger")) == 9_000_000


async def test_stream_has_correct_terminal_and_ledger(pg_db):
    async with gateway_environment(pg_db) as env:
        response = await env.client.post("/v1/chat/completions", json=dict(BODY, stream=True, stream_options={"include_usage": True}),
                                         headers={"Authorization": "Bearer "+env.secret})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert '"content":"Hello"' in response.text and '"prompt_tokens":4' in response.text
        assert response.text.endswith("data: [DONE]\n\n")


async def test_policy_quota_and_header_conflict_prevent_outbound_call(pg_db):
    async with gateway_environment(pg_db, quota=1) as env:
        response = await env.client.post("/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer "+env.secret})
        assert response.status_code == 429
        response = await env.client.post("/v1/chat/completions", json=dict(BODY, model="private"),
                                         headers={"Authorization": "Bearer "+env.secret})
        assert response.status_code == 403
        response = await env.client.post("/v1/chat/completions", json=BODY,
            headers={"Authorization": "Bearer "+env.secret, "x-api-key": "conflicting"})
        assert response.status_code == 401
        assert not env.upstream_requests


async def test_missing_usage_does_not_free_hold_or_emit_done(pg_db):
    from sqlalchemy import text
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(200, content=chat_success(False))) as env:
        response = await env.client.post("/v1/chat/completions", json=dict(BODY, stream=True),
                                         headers={"Authorization": "Bearer "+env.secret})
        assert "usage_pending" in response.text and "[DONE]" not in response.text
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT state FROM requests")) == "usage_pending"
            assert await session.scalar(text("SELECT held FROM quota_buckets WHERE window_kind='total'")) > 0


async def test_upstream_open_failure_is_http_error_without_fake_sse_success(pg_db):
    from sqlalchemy import text
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(401, json={"secret": "bad"})) as env:
        response = await env.client.post("/v1/chat/completions", json=dict(BODY, stream=True),
                                         headers={"Authorization": "Bearer "+env.secret})
        assert response.status_code == 502 and "bad" not in response.text
        assert len(env.upstream_requests) == 1
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT held FROM quota_buckets WHERE window_kind='total'")) == 0


async def test_models_filtered_and_query_keys_rejected(pg_db):
    async with gateway_environment(pg_db) as env:
        response = await env.client.get("/v1/models", headers={"Authorization": "Bearer "+env.secret})
        assert [model["id"] for model in response.json()["data"]] == ["public"]
        assert (await env.client.get("/v1/models?key="+env.secret)).status_code == 400


async def test_gateway_budget_holds_and_usage_cost_are_settled(pg_db):
    from decimal import Decimal
    from autobuild_json.gateway.metering.budgets import BudgetService
    from autobuild_json.gateway.routing.records import ProviderConfig
    async with gateway_environment(pg_db) as env:
        budgets = BudgetService(pg_db)
        budget = await budgets.create("USD", Decimal("1"))
        await env.engine.catalog.update_provider(env.provider_id, 1, ProviderConfig(name="budgeted", adapter="openai_compatible",
            root="https://provider.invalid/v1", budget_id=budget,
            cost_schedule={"currency": "USD", "input_per_million": "1", "output_per_million": "3"}))
        response = await env.client.post("/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer "+env.secret})
        assert response.status_code == 200, response.text
        spent, held, _ = await budgets.balance(budget)
        assert spent == Decimal("0.000019") and held == 0


async def test_public_health_does_not_reveal_registry_or_keys(pg_db):
    async with gateway_environment(pg_db) as env:
        result = await env.client.get("/health")
        assert result.status_code == 200
        assert result.json() == {"status": "ok"}
        assert env.secret not in result.text


async def test_disconnect_then_late_authoritative_usage_settles_once(pg_db):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import text
    from autobuild_json.gateway.identity.policy import Principal
    from autobuild_json.gateway.protocols.openai_chat import OpenAIChatCodec
    from autobuild_json.gateway.engine import RequestMeta
    from autobuild_json.gateway.maintenance import Maintenance

    async with gateway_environment(pg_db) as env:
        async with pg_db.sessions() as session:
            owner, version = (await session.execute(text(
                "SELECT customer_id,version FROM api_keys WHERE id=:id"), {"id": env.key_id})).one()
        now = datetime.now(timezone.utc)
        prepared = await env.engine.prepare(Principal(owner, env.key_id, version),
            OpenAIChatCodec().decode(BODY, {}), RequestMeta(__import__("uuid").uuid4(), now,
                                                           now + timedelta(seconds=60)))
        events = prepared.events()
        assert (await events.__anext__()).kind == "started"
        await events.aclose()
        await prepared.close()
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT state FROM requests")) == "usage_pending"
            assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 0
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0
            assert await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL")) == 0
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE attempts SET status='completed',usage=CAST(:usage AS jsonb) WHERE id=:id"),
                                  {"id": prepared.attempt_id,
                                   "usage": '{"input_tokens":4,"output_tokens":5}'})
            await session.execute(text("UPDATE requests SET deadline=now()-interval '30 seconds'"))
        maintenance = Maintenance(pg_db)
        assert await maintenance.tick() == 1
        assert await maintenance.tick() == 0
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT state FROM requests")) == "completed"
            assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 1
            assert await session.scalar(text("SELECT held FROM quota_buckets WHERE window_kind='total'")) == 0
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0
            assert await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL")) == 0
