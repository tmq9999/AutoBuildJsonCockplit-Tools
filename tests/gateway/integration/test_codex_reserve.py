"""Reserve entitlement is enforced on live routing, not just in the picker."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from autobuild_json.gateway.accounts.quota_client import parse_usage
from autobuild_json.gateway.admin.codex_models import PublishCodexModels, publish_codex_models
from autobuild_json.gateway.identity.policy import KeyPolicy
from autobuild_json.gateway.identity.service import IdentityService
from tests.gateway.codex_admin_support import account, private_env
from tests.gateway.unit.test_codex_reserve import reserve_payload
from .test_codex_pool_dispatch import BODY, codex_environment

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def store_quota(db, identity, *, eligible=True, age=0):
    payload = reserve_payload()
    payload['additional_rate_limits'][0]['rate_limit']['allowed'] = eligible
    quota = parse_usage(payload, identity, datetime.now(timezone.utc)-timedelta(seconds=age))
    async with db.sessions.begin() as session:
        await session.execute(text('INSERT INTO codex_account_state(credential_id,usage_snapshot,fetched_at) '
            'VALUES (:id,CAST(:quota AS jsonb),:at) ON CONFLICT(credential_id) DO UPDATE '
            'SET usage_snapshot=excluded.usage_snapshot,fetched_at=excluded.fetched_at'),
            {'id': identity, 'quota': quota.model_dump_json(), 'at': quota.fetched_at})


async def reserve_bindings(env):
    for binding in env.bindings:
        await env.engine.catalog.put_binding(replace(binding, upstream_model='gpt-reserve'))


@pytest.mark.parametrize('pool', [True, False])
async def test_reserve_skips_missing_or_denied_and_preserves_wire_model(pg_db, pool):
    async with codex_environment(pg_db, pool=pool) as env:
        await reserve_bindings(env)
        await store_quota(pg_db, env.bindings[0].credential_id, eligible=False)
        await store_quota(pg_db, env.bindings[2].credential_id)
        await env.engine.catalog.put_alias('reserve-alias', 'codex-test')
        result = await env.client.post('/v1/responses', json=dict(BODY, model='reserve-alias'), headers=env.headers)
        assert result.status_code == 200, result.text
        assert len(env.upstream_requests) == 1
        sent = env.upstream_requests[0]
        assert sent['headers']['chatgpt-account-id'] == 'account-2'
        assert sent['body']['model'] == 'gpt-reserve'


@pytest.mark.parametrize('mutation', ['missing', 'denied', 'stale', 'error'])
@pytest.mark.parametrize('pool', [True, False])
async def test_ineligible_reserve_never_dispatches_or_reserves_customer_quota(pg_db, pool, mutation):
    async with codex_environment(pg_db, pool=pool, count=1) as env:
        await reserve_bindings(env)
        if mutation != 'missing':
            await store_quota(pg_db, env.bindings[0].credential_id, eligible=mutation != 'denied',
                              age=121 if mutation == 'stale' else 0)
        if mutation == 'error':
            async with pg_db.sessions.begin() as session:
                await session.execute(text("UPDATE codex_account_state SET usage_error='codex_usage_unavailable'"))
        result = await env.client.post('/v1/responses', json=BODY, headers=env.headers)
        assert result.status_code == 503, result.text
        assert not env.upstream_requests
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT count(*) FROM requests')) == 0


async def test_eligible_account_outside_pool_is_not_a_fallback(pg_db):
    async with codex_environment(pg_db) as env:
        await reserve_bindings(env)
        await store_quota(pg_db, env.bindings[2].credential_id)
        await env.pools.put('codex-test', 1, env.policy.model_copy(update={'members': env.policy.members[:1]}), 'fixture')
        result = await env.client.post('/v1/responses', json=BODY, headers=env.headers)
        assert result.status_code == 503 and not env.upstream_requests


@pytest.mark.parametrize('native', [True, False])
async def test_pinned_reserve_rechecks_entitlement_without_account_fallback(pg_db, native):
    async with codex_environment(pg_db) as env:
        await reserve_bindings(env)
        for binding in env.bindings:
            await store_quota(pg_db, binding.credential_id)
        headers = env.headers if native else dict(env.headers, **{'X-Gateway-Session': 'reserve-session'})
        first = await env.client.post('/v1/responses', json=BODY, headers=headers)
        assert first.status_code == 200, first.text
        await store_quota(pg_db, env.bindings[0].credential_id, eligible=False)
        body = dict(BODY, previous_response_id=first.json()['id']) if native else BODY
        second = await env.client.post('/v1/responses', json=body, headers=headers)
        assert second.status_code >= 400, second.text
        assert len(env.upstream_requests) == 1


async def test_entitlement_removed_after_selection_is_rechecked_before_send(pg_db, monkeypatch):
    async with codex_environment(pg_db, count=1) as env:
        await reserve_bindings(env)
        await store_quota(pg_db, env.bindings[0].credential_id)
        original = env.ledger.mark_dispatched
        async def gate(*args, **kwargs):
            await original(*args, **kwargs)
            await store_quota(pg_db, env.bindings[0].credential_id, eligible=False)
        monkeypatch.setattr(env.ledger, 'mark_dispatched', gate)
        result = await env.client.post('/v1/responses', json=BODY, headers=env.headers)
        assert result.status_code >= 400 and not env.upstream_requests
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT held FROM quota_buckets WHERE window_kind='total'")) == 0


async def test_published_reserve_visible_without_eligibility_but_key_allowlist_applies(pg_db):
    async with codex_environment(pg_db, count=1) as env:
        services = SimpleNamespace(db=pg_db)
        first = await publish_codex_models(services, PublishCodexModels(model_ids=['gpt-reserve']))
        assert first['models_created'] == 1 and first['bindings_created'] == 1
        again = await publish_codex_models(services, PublishCodexModels(model_ids=['gpt-reserve']))
        assert again['models_created'] == again['bindings_created'] == 0
        models = (await env.client.get('/v1/models', headers=env.headers)).json()['data']
        assert 'gpt-reserve' not in [m['id'] for m in models]
        await IdentityService(pg_db, b'p'*32).update_policy(env.key_id, 2,
            KeyPolicy(model_ids={'gpt-reserve'}, protocols={'openai'}, total_micro=10**12))
        models = (await env.client.get('/v1/models', headers=env.headers)).json()['data']
        assert [m['id'] for m in models] == ['gpt-reserve']
        result = await env.client.post('/v1/responses', json=dict(BODY, model='gpt-reserve'), headers=env.headers)
        assert result.status_code == 503 and not env.upstream_requests


async def test_refresh_persists_and_projects_additional_limits_without_secrets(pg_db, settings):
    calls = []
    def reply(request):
        calls.append(request.url.path)
        payload = reserve_payload() if request.url.path.endswith('/usage') else {'available_count': 0}
        payload['untrusted_secret'] = 'NEVER-RETAIN-THIS'
        return httpx.Response(200, json=payload)
    async with private_env(pg_db, settings, reply) as (client, services):
        identity = await account(services)
        path = f'/api/service/oauth-accounts/{identity}/quota'
        result = await client.post(path+'/refresh', json={})
        assert result.status_code == 200, result.text
        quota = await client.get(path)
        data = quota.json()
        assert data['stale'] is False
        assert data['snapshot']['allowed'] is False
        limit = data['snapshot']['additional_rate_limits'][0]
        assert limit['model_id'] == 'gpt-reserve' and limit['allowed'] is True
        assert limit['primary']['used_percent'] == '25'
        assert len(calls) == 2  # Storage GET never probes upstream.
        assert 'NEVER-RETAIN-THIS' not in quota.text
        async with pg_db.sessions() as session:
            saved = await session.scalar(text('SELECT usage_snapshot FROM codex_account_state WHERE credential_id=:id'), {'id': identity})
            assert saved['additional_rate_limits'][0] == limit
            assert 'NEVER-RETAIN-THIS' not in json.dumps(saved)
