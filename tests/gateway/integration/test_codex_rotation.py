"""Full-pool selection with bounded, pre-generation account failover."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text

from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec
from autobuild_json.gateway.routing.pool_records import AccountPoolPolicy
from .test_codex_pool_dispatch import BODY, codex_environment
from .test_responses_gateway import native_wire

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_reaches_eighth_distinct_account_and_charges_success_once(pg_db):
    def respond(request):
        index = int(request.headers['chatgpt-account-id'].split('-')[-1])
        if index < 7:
            return httpx.Response(400, json={'detail': "The 'fixture' model is not supported with this account."})
        return httpx.Response(200, content=native_wire())
    async with codex_environment(pg_db, count=10, respond=respond) as env:
        # Duplicate bindings must not consume attempts or account weight.
        await env.engine.catalog.put_binding(replace(env.bindings[0], id=UUID(int=99)))
        response = await env.client.post('/v1/responses', json=BODY, headers=env.headers)
        assert response.status_code == 200, response.text
        assert [r['headers']['chatgpt-account-id'] for r in env.upstream_requests] == [f'account-{i}' for i in range(8)]
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM attempts WHERE status='rejected'")) == 7
            assert await session.scalar(text("SELECT count(*) FROM attempts WHERE status='completed'")) == 1
            assert (await session.execute(text('SELECT count(*),sum(amount_micro) FROM usage_ledger'))).one() == (1, 23)
            assert (await session.execute(text('SELECT input_bound,output_bound FROM requests'))).one() == (1700, 1200)
            assert await session.scalar(text("SELECT held FROM quota_buckets WHERE window_kind='total'")) == 0
            assert await session.scalar(text('SELECT count(*) FROM provider_admissions WHERE active')) == 0
            assert await session.scalar(text('SELECT count(*) FROM model_bindings WHERE cooldown_until>now()')) == 8


async def test_exhausted_attempt_budget_never_dispatches_ninth_account(pg_db):
    async with codex_environment(pg_db, count=10, respond=lambda _: httpx.Response(401)) as env:
        response = await env.client.post('/v1/responses', json=BODY, headers=env.headers)
        assert response.status_code == 502
        assert len(env.upstream_requests) == 8
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT state FROM requests")) == 'released'
            assert (await session.execute(text('SELECT count(*),sum(amount_micro) FROM usage_ledger'))).one() == (1, 0)
            assert await session.scalar(text('SELECT count(*) FROM credentials WHERE cooldown_until>now()')) == 8


async def test_round_robin_dynamic_pool_serves_all_accounts_over_requests(pg_db):
    async with codex_environment(pg_db, count=10) as env:
        # Explicit round-robin must not starve higher-priority-number bindings
        # or overweight an account with duplicate bindings.
        await env.engine.catalog.put_binding(replace(env.bindings[0], id=UUID(int=99)))
        await env.pools.put('codex-test', 1, AccountPoolPolicy(mode='round_robin', all_accounts=True), 'fixture')
        for _ in range(11):
            response = await env.client.post('/v1/responses', json=BODY, headers=env.headers)
            assert response.status_code == 200, response.text
        assert [r['headers']['chatgpt-account-id'] for r in env.upstream_requests] == [f'account-{i}' for i in range(10)] + ['account-0']
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT count(DISTINCT credential_id) FROM attempts')) == 10
            assert (await session.execute(text('SELECT count(*),sum(amount_micro) FROM usage_ledger'))).one() == (11, 253)


@pytest.mark.parametrize('code', ['reauth_required', 'refresh_uncertain'])
async def test_failed_refresh_skips_account_before_inference_without_unfencing_it(pg_db, monkeypatch, code):
    async with codex_environment(pg_db) as env:
        refresh = env.engine.credentials.fresh_tokens
        calls = []
        async def fail_first(credential, deadline):
            calls.append(credential)
            if credential == env.bindings[0].credential_id:
                async with pg_db.sessions.begin() as session:
                    await session.execute(text('UPDATE credentials SET health=:health WHERE id=:id'),
                                          {'id': credential, 'health': code})
                raise GatewayError(code, 503, 'refresh')
            return await refresh(credential, deadline)
        monkeypatch.setattr(env.engine.credentials, 'fresh_tokens', fail_first)
        response = await env.client.post('/v1/responses', json=BODY, headers=env.headers)
        assert response.status_code == 200, response.text
        assert len(calls) == 2
        assert [r['headers']['chatgpt-account-id'] for r in env.upstream_requests] == ['account-1']
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT health FROM credentials WHERE id=:id'),
                                        {'id': env.bindings[0].credential_id}) == code
            assert await session.scalar(text('SELECT last_error FROM codex_account_state WHERE credential_id=:id'),
                                        {'id': env.bindings[0].credential_id}) == code
            assert await session.scalar(text('SELECT count(*) FROM attempts')) == 1
            assert await session.scalar(text('SELECT sum(amount_micro) FROM usage_ledger')) == 23


async def test_model_cooldown_is_shared_by_duplicate_bindings_but_not_other_models(pg_db):
    def respond(request):
        if request.headers['chatgpt-account-id'] == 'account-0':
            return httpx.Response(400, json={'detail': 'The requested model is not supported for this account.'})
        return httpx.Response(200, content=native_wire())
    async with codex_environment(pg_db, respond=respond) as env:
        other = replace(env.bindings[0], id=UUID(int=90), upstream_model='other-model')
        await env.engine.catalog.put_binding(other)
        response = await env.client.post('/v1/responses', json=BODY, headers=env.headers)
        assert response.status_code == 200
        async with pg_db.sessions() as session:
            rows = dict((await session.execute(text('SELECT id,cooldown_until FROM model_bindings'))).all())
            assert rows[env.bindings[0].id] is not None
            assert rows[other.id] is None
            assert await session.scalar(text('SELECT cooldown_until FROM credentials WHERE id=:id'),
                                        {'id': env.bindings[0].credential_id}) is None
        # Only the allowed alternate binding on account-0 remains a candidate.
        routes = await env.engine.catalog.candidates(env.principal, ResponsesCodec().decode(BODY, {}))
        assert env.bindings[0].id not in {r.binding_id for r in routes}
        assert other.id in {r.binding_id for r in routes}


@pytest.mark.parametrize('status', [400, 403, 422, 500, 503])
async def test_generic_or_ambiguous_errors_never_walk_pool(pg_db, status):
    async with codex_environment(pg_db, respond=lambda _: httpx.Response(status, json={'detail': 'private-value'})) as env:
        response = await env.client.post('/v1/responses', json=BODY, headers=env.headers)
        assert response.status_code == 502 and 'private-value' not in response.text
        assert len(env.upstream_requests) == 1
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT state FROM requests')) == ('usage_pending' if status >= 500 else 'released')


async def test_expired_deadline_prevents_any_refresh_or_dispatch(pg_db, monkeypatch):
    async with codex_environment(pg_db) as env:
        async def unexpected(*args):
            pytest.fail('refresh attempted after deadline')
        monkeypatch.setattr(env.engine.credentials, 'fresh_tokens', unexpected)
        meta = replace(env.engine.meta(BODY), deadline=datetime.now(timezone.utc)-timedelta(seconds=1))
        with pytest.raises(GatewayError):
            await env.engine.prepare(env.principal, ResponsesCodec().decode(BODY, {}), meta)
        assert not env.upstream_requests


async def test_explicit_retry_zero_keeps_model_rejection_single_attempt(pg_db):
    async with codex_environment(pg_db, respond=lambda _: httpx.Response(400, json={'detail': 'model is not supported'})) as env:
        await env.pools.put('codex-test', 1, env.policy.model_copy(update={'retry_limit': 0}), 'fixture')
        response = await env.client.post('/v1/responses', json=BODY, headers=env.headers)
        assert response.status_code == 502
        assert len(env.upstream_requests) == 1


@pytest.mark.parametrize('missing', [False, True])
async def test_retry_after_covers_all_accounts_not_just_the_final_rejection(pg_db, missing):
    def respond(request):
        index = int(request.headers['chatgpt-account-id'].split('-')[-1])
        return httpx.Response(429, headers={} if missing and index == 0 else {'retry-after': str(60 + index * 60)})
    async with codex_environment(pg_db, respond=respond) as env:
        response = await env.client.post('/v1/responses', json=BODY, headers=env.headers)
        assert response.status_code == 429
        assert len(env.upstream_requests) == 3
        if missing:
            assert 'retry-after' not in response.headers
        else:
            assert 55 <= int(response.headers['retry-after']) <= 60


async def test_untried_account_does_not_inherit_last_account_retry_after(pg_db):
    async with codex_environment(pg_db, count=10, respond=lambda _: httpx.Response(429, headers={'retry-after': '120'})) as env:
        response = await env.client.post('/v1/responses', json=BODY, headers=env.headers)
        assert response.status_code == 429 and 'retry-after' not in response.headers
        assert len(env.upstream_requests) == 8
