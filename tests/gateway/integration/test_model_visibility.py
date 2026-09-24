import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from autobuild_json.gateway.identity.policy import KeyPolicy
from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.identity.service import IdentityService
from autobuild_json.gateway.routing.records import ModelConfig
from tests.gateway.harness import gateway_environment
from .test_codex_pool_dispatch import codex_environment

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def prefixed(env, *, version=1, allowed=('public',), all_models=False):
    identity = IdentityService(env.engine.db, b'p'*32)
    await identity.update_policy(env.key_id, version, KeyPolicy(model_ids=set(allowed), all_models=all_models,
        excluded_model_ids={'blocked'}, model_prefix='team/',
        protocols={'openai', 'anthropic', 'gemini', 'ollama'}, total_micro=10**12, concurrency=10))
    return {'Authorization': 'Bearer '+env.secret}


async def test_lists_exclusions_and_raw_names_share_policy(pg_db):
    async with gateway_environment(pg_db) as env:
        await env.engine.catalog.put_model(ModelConfig(model_id='blocked', identity='blocked', enabled=True))
        headers = await prefixed(env, all_models=True)
        assert (await env.client.get('/v1/models', headers=headers)).json()['data'] == [
            {'id': 'team/public', 'object': 'model', 'created': 0, 'owned_by': 'gateway'}]
        assert (await env.client.get('/v1beta/models', headers=headers)).json()['models'][0]['name'] == 'models/team/public'
        assert (await env.client.get('/api/tags', headers=headers)).json() == {'models': [{'name': 'team/public', 'model': 'team/public'}]}
        for model in ['public', 'team/blocked', 'team/team/public', 'other/team/public']:
            result = await env.client.post('/v1/responses', headers=headers, json={'model': model, 'input': 'Hi'})
            assert result.status_code in {403, 404}, (model, result.text)
        assert not env.upstream_requests


@pytest.mark.parametrize('protocol', ['openai', 'responses', 'anthropic', 'gemini', 'ollama'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('max_length', [False, True])
async def test_alias_has_visible_canonical_response_and_canonical_accounting(pg_db, protocol, stream, max_length):
    async with gateway_environment(pg_db) as env:
        await env.engine.catalog.put_alias('shortcut', 'public')
        headers = await prefixed(env)
        model = 'team/shortcut'
        canonical, visible = 'public', 'team/public'
        if max_length:
            canonical, prefix = 'm'*200, 't'*63+'/'
            await env.engine.catalog.put_model(ModelConfig(model_id=canonical, identity='identity-A', enabled=True))
            await env.engine.catalog.put_binding(replace(env.binding, id=uuid4(), public_model_id=canonical))
            await IdentityService(pg_db, b'p'*32).update_policy(env.key_id, 2, KeyPolicy(model_ids={canonical},
                model_prefix=prefix, protocols={'openai', 'anthropic', 'gemini', 'ollama'}, total_micro=10**12))
            model = visible = prefix+canonical
            assert (await env.client.get('/v1/models', headers=headers)).json()['data'][0]['id'] == visible
        body = {'model': model, 'messages': [{'role': 'user', 'content': 'Hi'}], 'stream': stream}
        path = '/v1/chat/completions'
        if protocol == 'responses':
            path, body = '/v1/responses', {'model': model, 'input': 'Hi', 'stream': stream}
        elif protocol == 'anthropic':
            path = '/v1/messages'
            headers['anthropic-version'] = '2023-06-01'
            body['max_tokens'] = 8
        elif protocol == 'gemini':
            path = '/v1beta/models/'+model+(':streamGenerateContent?alt=sse' if stream else ':generateContent')
            body = {'contents': [{'role': 'user', 'parts': [{'text': 'Hi'}]}]}
        elif protocol == 'ollama':
            path = '/api/chat'
        result = await env.client.post(path, json=body, headers=headers)
        assert result.status_code == 200, result.text
        if stream:
            frames = [json.loads(line.removeprefix('data: ')) for line in result.text.splitlines()
                      if (line.startswith('data: ') and line != 'data: [DONE]') or (protocol == 'ollama' and line.startswith('{'))]
            names = [frame.get('model', frame.get('modelVersion', frame.get('response', frame.get('message', {})).get('model')))
                     for frame in frames]
            assert visible in names
            assert all(name in {None, visible} for name in names)
        else:
            assert result.json().get('model', result.json().get('modelVersion')) == visible
        assert env.upstream_requests[0]['body']['model'] == 'upstream'
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT model_id FROM requests')) == canonical


async def test_prefixed_codex_native_continuation_and_header_pin_use_canonical_scope(pg_db):
    async with codex_environment(pg_db) as env:
        await env.engine.catalog.put_alias('shortcut', 'codex-test')
        headers = await prefixed(env, version=2, allowed=('codex-test',))
        headers['X-Gateway-Session'] = 'private-session'
        first = await env.client.post('/v1/responses', headers=headers, json={'model': 'team/shortcut', 'input': 'Hi'})
        assert first.status_code == 200, first.text
        second = await env.client.post('/v1/responses', headers=headers,
            json={'model': 'team/codex-test', 'input': 'Next', 'previous_response_id': first.json()['id']})
        assert second.status_code == 200, second.text
        assert first.json()['model'] == second.json()['model'] == 'team/codex-test'
        assert [r['headers']['chatgpt-account-id'] for r in env.upstream_requests] == ['account-0', 'account-0']
        assert env.upstream_requests[1]['body']['previous_response_id'] == 'upstream-resp-private'
        async with pg_db.sessions() as session:
            assert list((await session.execute(text('SELECT DISTINCT model_id FROM requests'))).scalars()) == ['codex-test']
            assert await session.scalar(text('SELECT model_id FROM account_session_bindings')) == 'codex-test'


@pytest.mark.parametrize('alias', ['wild*', 'bad name', 'a/../public', 'a//public', 'a\n', 'é', 'public?'])
async def test_ambiguous_alias_names_fail_validation(pg_db, alias):
    async with gateway_environment(pg_db) as env:
        with pytest.raises(GatewayError, match='invalid_request'):
            await env.engine.catalog.put_alias(alias, 'public')


async def test_excluded_alias_target_and_alias_only_permission_never_expand_access(pg_db):
    async with gateway_environment(pg_db) as env:
        await env.engine.catalog.put_alias('shortcut', 'public')
        await env.engine.catalog.put_model(ModelConfig(model_id='blocked', identity='blocked', enabled=True))
        await env.engine.catalog.put_alias('friendly', 'blocked')
        headers = await prefixed(env, allowed=('shortcut',))
        for model in ['team/shortcut', 'team/friendly']:
            result = await env.client.post('/v1/responses', headers=headers, json={'model': model, 'input': 'Hi'})
            assert result.status_code == 403
        assert (await env.client.get('/v1/models', headers=headers)).json()['data'] == []
        assert not env.upstream_requests


async def test_exact_once_prefix_removal_preserves_slash_colon_canonical_identity(pg_db):
    async with gateway_environment(pg_db) as env:
        catalog = env.engine.catalog
        # Canonical names may themselves start with a key's prefix. No repeated stripping.
        await catalog.put_model(ModelConfig(model_id='team/public:v2', identity='identity-A', enabled=True))
        await catalog.put_binding(replace(env.binding, id=uuid4(), public_model_id='team/public:v2'))
        await catalog.put_alias('shortcut:v2', 'team/public:v2')
        headers = await prefixed(env, allowed=('team/public:v2',))
        for model in ['team/team/public:v2', 'team/shortcut:v2']:
            result = await env.client.post('/v1beta/models/'+model+':generateContent', headers=headers,
                json={'contents': [{'parts': [{'text': 'Hi'}]}]})
            assert result.status_code == 200, result.text
            assert result.json()['modelVersion'] == 'team/team/public:v2'
        denied = await env.client.post('/v1/responses', headers=headers, json={'model': 'team/public:v2', 'input': 'Hi'})
        assert denied.status_code == 403
        principal = await IdentityService(pg_db, b'p'*32).authenticate(env.secret, 'openai')
        assert [m.model_id for m in await catalog.list_models(principal)] == ['team/public:v2']
        async with pg_db.sessions() as session:
            assert list((await session.execute(text('SELECT DISTINCT model_id FROM requests'))).scalars()) == ['team/public:v2']


async def test_two_key_prefixes_share_canonical_model_but_not_continuations(pg_db):
    async with codex_environment(pg_db) as env:
        first_headers = await prefixed(env, version=2, allowed=('codex-test',))
        identity = IdentityService(pg_db, b'p'*32)
        second = await identity.create_key(env.principal.customer_id, KeyPolicy(model_ids={'codex-test'},
            model_prefix='other/', protocols={'openai'}, total_micro=10**12))
        second_headers = {'Authorization': 'Bearer '+second.secret}
        first = await env.client.post('/v1/responses', headers=first_headers, json={'model': 'team/codex-test', 'input': 'Hi'})
        assert first.status_code == 200
        other = await env.client.post('/v1/responses', headers=second_headers, json={'model': 'other/codex-test', 'input': 'Hi'})
        assert other.status_code == 200 and other.json()['model'] == 'other/codex-test'
        denied = await env.client.post('/v1/responses', headers=second_headers,
            json={'model': 'other/codex-test', 'input': 'Next', 'previous_response_id': first.json()['id']})
        assert denied.status_code == 400 and denied.json()['error']['code'] == 'invalid_state'
        assert len(env.upstream_requests) == 2


async def test_canonical_pool_scope_still_enforces_exclusions(pg_db):
    from autobuild_json.gateway.routing.pool_records import PoolScope
    async with codex_environment(pg_db) as env:
        headers = await prefixed(env, version=2, allowed=('codex-test',))
        headers['X-Gateway-Session'] = 'private-session'
        result = await env.client.post('/v1/responses', headers=headers, json={'model': 'team/codex-test', 'input': 'Hi'})
        assert result.status_code == 200
        identity = IdentityService(pg_db, b'p'*32)
        principal = await identity.authenticate(env.secret, 'openai')
        digest = await env.engine.session_digest(principal, 'team/codex-test', 'private-session')
        scope = PoolScope(customer_id=principal.customer_id, key_id=principal.key_id,
                          model_id='codex-test', session_digest=digest)
        await identity.update_policy(env.key_id, 3, KeyPolicy(all_models=True, model_prefix='team/',
            excluded_model_ids={'codex-test'}, protocols={'openai'}, total_micro=10**12))
        async with pg_db.sessions.begin() as session:
            with pytest.raises(GatewayError, match='permission_denied'):
                await env.pools._scope(session, scope)
        assert len(env.upstream_requests) == 1


@pytest.mark.parametrize('protocol', ['anthropic', 'gemini'])
async def test_prefixed_native_count_preserves_raw_count_and_zero_charge(pg_db, protocol):
    import httpx
    from autobuild_json.gateway.routing.records import ProviderConfig
    response = {'input_tokens': 17} if protocol == 'anthropic' else {'totalTokens': 17}
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(200, json=response)) as env:
        headers = await prefixed(env)
        await env.engine.catalog.update_provider(env.provider_id, 1, ProviderConfig(name='native', adapter=protocol,
            root='https://provider.invalid/v1', auth_mode='x-api-key' if protocol == 'anthropic' else 'x-goog-api-key'))
        if protocol == 'anthropic':
            headers['anthropic-version'] = '2023-06-01'
            path, body = '/v1/messages/count_tokens', {'model': 'team/public', 'messages': [{'role': 'user', 'content': 'Hi'}]}
        else:
            path, body = '/v1beta/models/team/public:countTokens', {'contents': [{'parts': [{'text': 'Hi'}]}]}
        result = await env.client.post(path, headers=headers, json=body)
        assert result.status_code == 200 and result.json() == response
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT amount_micro FROM usage_ledger')) == 0
            assert await session.scalar(text('SELECT model_id FROM requests')) == 'public'


async def test_direct_ledger_admission_rejects_excluded_canonical_model(pg_db):
    from autobuild_json.gateway.metering.records import Admission, Bounds
    async with gateway_environment(pg_db) as env:
        identity = IdentityService(pg_db, b'p'*32)
        await identity.update_policy(env.key_id, 1, KeyPolicy(all_models=True, excluded_model_ids={'public'},
            model_prefix='team/', protocols={'openai'}, total_micro=10**12))
        principal = await identity.authenticate(env.secret, 'openai')
        with pytest.raises(GatewayError, match='permission_denied'):
            await env.ledger.reserve(Admission(uuid4(), principal, 'public', Bounds(1, 1), 1, 1,
                datetime.now(timezone.utc)+timedelta(seconds=60)))
        result = await env.client.post('/v1/responses', headers={'Authorization': 'Bearer '+env.secret},
                                       json={'model': 'team/public', 'input': 'Hi'})
        assert result.status_code == 403 and not env.upstream_requests
