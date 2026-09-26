from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from tests.gateway.harness import gateway_environment
from .test_responses_gateway import native_wire
from .test_oauth_import import record, credential_service

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_codex_route_uses_account_tokens_and_normalizes_generation_controls(pg_db):
    from autobuild_json.gateway.routing.records import ModelConfig, BindingConfig
    from autobuild_json.gateway.identity.policy import KeyPolicy
    from autobuild_json.gateway.identity.service import IdentityService
    from autobuild_json.gateway.accounts.imports import CODEX_PROVIDER
    async def exchange(tokens, lease, deadline):
        return {"id_token": "verified-id", "access_token": "verified-access", "refresh_token": "verified-refresh", "expires_in": 3600}
    async def verify(*args):
        return None
    service = await credential_service(pg_db, exchange, verify)
    credential = await service.import_record(record(), None)
    await service.fresh_tokens(credential, datetime.now(timezone.utc)+timedelta(seconds=60))
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(200, content=native_wire())) as env:
        env.engine.credentials = service
        await env.engine.catalog.put_model(ModelConfig(model_id="codex-test", identity="codex-test", enabled=True))
        await env.engine.catalog.put_binding(BindingConfig(id=uuid4(), provider_id=CODEX_PROVIDER, credential_id=credential,
            public_model_id="codex-test", upstream_model="codex-test", identity="codex-test", input_bound=1000, output_bound=500))
        await IdentityService(pg_db, b"p"*32).update_policy(env.key_id, 1,
            KeyPolicy(model_ids={"codex-test"}, protocols={"openai"}, total_micro=10**12))
        headers = {"Authorization": "Bearer "+env.secret}
        response = await env.client.post("/v1/responses", json={"model": "codex-test", "input": "hi", "max_output_tokens": 1,
            "temperature": 0.5, "top_p": 0.9}, headers=headers)
        assert response.status_code == 200, response.text
        outbound = env.upstream_requests[0]
        assert outbound["headers"]["authorization"] == "Bearer verified-access"
        assert outbound["headers"]["chatgpt-account-id"] == "account-test"
        assert outbound["headers"]["accept"] == "text/event-stream"
        assert outbound["headers"]["originator"] == "codex-tui"
        assert outbound["headers"]["user-agent"].startswith("codex-tui/")
        assert not set(outbound["body"]) & {"max_output_tokens", "temperature", "top_p"}
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT output_bound FROM requests")) == 500


@pytest.mark.parametrize("client_cap", [1, 10000])
@pytest.mark.parametrize("retry", [False, True])
async def test_codex_hints_reserve_certified_bounds_and_bill_exact_cached_usage(pg_db, monkeypatch, client_cap, retry):
    from .test_codex_pool_dispatch import codex_environment
    # Cached input is a subset of input; reasoning is a subset of output.
    wire = native_wire().replace(
        b'"input_tokens": 4, "output_tokens": 5, "total_tokens": 9',
        b'"input_tokens": 4, "output_tokens": 5, "total_tokens": 9, '
        b'"input_tokens_details": {"cached_tokens": 3}, "output_tokens_details": {"reasoning_tokens": 2}')
    def respond(request):
        if retry and request.headers["chatgpt-account-id"] == "account-0":
            return httpx.Response(429)
        return httpx.Response(200, content=wire)
    async with codex_environment(pg_db, respond=respond) as env:
        holds = []
        reserve, resize = env.ledger.reserve, env.ledger.resize
        async def capture_reserve(admission):
            hold = await reserve(admission)
            holds.append((admission.bounds.input_tokens, admission.bounds.output_tokens, hold.amount_micro))
            return hold
        async def capture_resize(hold, bounds):
            resized = await resize(hold, bounds)
            holds.append((bounds.input_tokens, bounds.output_tokens, resized.amount_micro))
            return resized
        monkeypatch.setattr(env.ledger, "reserve", capture_reserve)
        monkeypatch.setattr(env.ledger, "resize", capture_resize)
        response = await env.client.post("/v1/responses", json={"model": "codex-test", "input": "hi",
            "max_output_tokens": client_cap, "temperature": 0.5, "top_p": 0.9}, headers=env.headers)
        assert response.status_code == 200, response.text
        assert holds == ([(1000, 500, 5500), (1100, 600, 6200)] if retry else [(1000, 500, 5500)])
        assert len(env.upstream_requests) == (2 if retry else 1)
        assert all(not set(r["body"]) & {"max_output_tokens", "temperature", "top_p"} for r in env.upstream_requests)
        assert response.json()["usage"] == {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9,
            "input_tokens_details": {"cached_tokens": 3}, "output_tokens_details": {"reasoning_tokens": 2}}
        async with pg_db.sessions() as session:
            assert (await session.execute(text("SELECT state,input_bound,output_bound FROM requests"))).one() == (
                "completed", 1100 if retry else 1000, 600 if retry else 500)
            assert (await session.execute(text("SELECT count(*),sum(amount_micro) FROM usage_ledger"))).one() == (1, 20)
            assert (await session.execute(text("SELECT spent,held FROM quota_buckets WHERE window_kind='total'"))).one() == (20, 0)


async def test_codex_small_client_hint_cannot_bypass_full_quota_admission(pg_db):
    from .test_codex_pool_dispatch import codex_environment
    from autobuild_json.gateway.identity.policy import KeyPolicy
    from autobuild_json.gateway.identity.service import IdentityService
    async with codex_environment(pg_db) as env:
        await IdentityService(pg_db, b"p"*32).update_policy(env.key_id, 2,
            KeyPolicy(model_ids={"codex-test"}, protocols={"openai"}, total_micro=4003))
        response = await env.client.post("/v1/responses", json={"model": "codex-test", "input": "hi",
            "max_output_tokens": 1}, headers=env.headers)
        assert response.status_code == 429, response.text
        assert response.json()["error"]["code"] == "quota_exceeded"
        assert not env.upstream_requests
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM requests")) == 0


async def test_openai_generation_cap_remains_enforced_and_forwarded(pg_db):
    async with gateway_environment(pg_db) as env:
        headers = {"Authorization": "Bearer " + env.secret}
        response = await env.client.post("/v1/responses", json={"model": "public", "input": "hi",
            "max_output_tokens": 8, "temperature": 0.5, "top_p": 0.9}, headers=headers)
        assert response.status_code == 200, response.text
        outbound = env.upstream_requests[0]["body"]
        assert outbound["max_completion_tokens"] == 8 and outbound["temperature"] == 0.5 and outbound["top_p"] == 0.9
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT output_bound FROM requests")) == 8
        rejected = await env.client.post("/v1/responses", json={"model": "public", "input": "hi",
            "max_output_tokens": 501}, headers=headers)
        assert rejected.status_code == 400
        assert len(env.upstream_requests) == 1
