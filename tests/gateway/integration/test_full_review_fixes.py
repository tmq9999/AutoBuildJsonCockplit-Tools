"""Regressions for the September gateway review; PostgreSQL state is real."""
from uuid import uuid4
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import text

from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.identity.policy import KeyPolicy
from autobuild_json.gateway.identity.service import IdentityService
from autobuild_json.gateway.routing.pool_records import PoolMember
from autobuild_json.gateway.routing.records import ProviderConfig
from tests.gateway.harness import gateway_environment
from .test_anthropic_gateway import enable_anthropic
from .test_codex_pool_dispatch import BODY, codex_environment
from .test_responses_gateway import native_wire

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_disabling_affinity_releases_existing_session_pin(pg_db):
    async with codex_environment(pg_db) as env:
        headers = dict(env.headers, **{"X-Gateway-Session": "review-session"})
        assert (await env.client.post('/v1/responses', json=BODY, headers=headers)).status_code == 200
        assert env.upstream_requests[-1]['headers']['chatgpt-account-id'] == 'account-0'
        policy = env.policy.model_copy(update={'session_affinity': False, 'members': tuple(
            PoolMember(credential_id=b.credential_id, priority=2-i) for i, b in enumerate(env.bindings))})
        await env.pools.put('codex-test', 1, policy, 'test')
        result = await env.client.post('/v1/responses', json=BODY, headers=headers)
        assert result.status_code == 200, result.text
        assert env.upstream_requests[-1]['headers']['chatgpt-account-id'] == 'account-2'


async def test_codex_401_expires_access_token_and_next_request_refreshes(pg_db):
    def respond(request):
        return (httpx.Response(200, content=native_wire())
                if request.headers['authorization'] == 'Bearer refreshed-access' else httpx.Response(401))
    async with codex_environment(pg_db, count=1, respond=respond) as env:
        async def exchange(tokens, lease, deadline):
            return {'id_token': 'refreshed-id', 'access_token': 'refreshed-access',
                    'refresh_token': 'refreshed-refresh', 'expires_in': 3600}
        async def verify(*args):
            pass
        env.engine.credentials.exchange = exchange
        env.engine.credentials.verifier = verify
        first = await env.client.post('/v1/responses', json=BODY, headers=env.headers)
        assert first.status_code == 502
        async with pg_db.sessions.begin() as session:
            assert await session.scalar(text('SELECT token_expires_at<now() FROM credentials WHERE id=:id'),
                                        {'id': env.bindings[0].credential_id})
            assert await session.scalar(text('SELECT cooldown_until>now() FROM credentials WHERE id=:id'),
                                        {'id': env.bindings[0].credential_id})
            assert (await session.execute(text('SELECT failure_count,last_error FROM codex_account_state '
                'WHERE credential_id=:id'), {'id': env.bindings[0].credential_id})).one() == (1, 'upstream_error')
            await session.execute(text('UPDATE credentials SET cooldown_until=NULL WHERE id=:id'),
                                  {'id': env.bindings[0].credential_id})
        second = await env.client.post('/v1/responses', json=BODY, headers=env.headers)
        assert second.status_code == 200, second.text
        assert len(env.upstream_requests) == 2
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT token_generation FROM credentials WHERE id=:id'),
                                        {'id': env.bindings[0].credential_id}) == 1
            assert await session.scalar(text('SELECT sum(amount_micro) FROM usage_ledger')) == 23


@pytest.mark.parametrize('operation', ['create', 'update'])
async def test_provider_rejects_missing_proxy_atomically(pg_db, operation):
    async with gateway_environment(pg_db) as env:
        config = ProviderConfig(name='Review', adapter='openai_compatible',
                                root='https://provider.invalid/v1', proxy_profile_id=uuid4())
        with pytest.raises(GatewayError, match='invalid_request'):
            if operation == 'create':
                await env.engine.catalog.create_provider(config)
            else:
                await env.engine.catalog.update_provider(env.provider_id, 1, config)
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT count(*) FROM providers')) == 1
            assert await session.scalar(text('SELECT version FROM providers')) == 1


async def test_token_count_closes_attempt_and_records_no_generation(pg_db):
    async with gateway_environment(pg_db, respond=lambda _: httpx.Response(200, json={'input_tokens': 17})) as env:
        headers = await enable_anthropic(env)
        await env.engine.catalog.update_provider(env.provider_id, 1, ProviderConfig(
            name='Counter', adapter='anthropic', root='https://provider.invalid/v1'))
        result = await env.client.post('/v1/messages/count_tokens',
            json={'model': 'public', 'messages': [{'role': 'user', 'content': 'hi'}]}, headers=headers)
        assert result.status_code == 200 and result.json() == {'input_tokens': 17}
        async with pg_db.sessions() as session:
            assert (await session.execute(text("SELECT status,usage->>'input_tokens',usage->>'output_tokens' FROM attempts"))).one() == ('completed', '0', '0')
            assert await session.scalar(text('SELECT sum(amount_micro) FROM usage_ledger')) == 0


async def test_token_count_egress_rejection_is_not_dispatched(pg_db):
    async with gateway_environment(pg_db) as env:
        headers = await enable_anthropic(env)
        await env.engine.catalog.update_provider(env.provider_id, 1, ProviderConfig(
            name='Counter', adapter='anthropic', root='https://provider.invalid/v1'))
        async def private_address(*args):
            return ['127.0.0.1']
        env.engine.transport.policy.resolver = private_address
        result = await env.client.post('/v1/messages/count_tokens',
            json={'model': 'public', 'messages': []}, headers=headers)
        assert result.status_code == 502
        assert not env.upstream_requests
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT count(*) FROM attempts')) == 0
            assert await session.scalar(text('SELECT state FROM requests')) == 'released'


@pytest.mark.parametrize('body', ['{', 'null', '[]', '1', '{"model": []}'])
async def test_ollama_show_invalid_body_returns_safe_client_error(pg_db, body):
    async with gateway_environment(pg_db) as env:
        await IdentityService(pg_db, b'p'*32).update_policy(env.key_id, 1, KeyPolicy(
            model_ids={'public'}, protocols={'ollama'}))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app, raise_app_exceptions=False),
                                     base_url='http://127.0.0.1:8788') as client:
            response = await client.post('/api/show', content=body,
                headers={'Authorization': 'Bearer '+env.secret, 'Content-Type': 'application/json'})
        assert response.status_code == 400
        assert response.json() == {'error': 'invalid_request'}


@pytest.mark.parametrize('path', ['/v1/models', '/models?client_version=1', '/v1beta/models', '/api/tags', '/api/version'])
async def test_authenticated_catalog_reads_cannot_be_cached(pg_db, path):
    async with gateway_environment(pg_db) as env:
        await IdentityService(pg_db, b'p'*32).update_policy(env.key_id, 1, KeyPolicy(
            model_ids={'public'}, protocols={'openai', 'gemini', 'ollama'}))
        response = await env.client.get(path, headers={'Authorization': 'Bearer '+env.secret})
        assert response.status_code == 200
        assert response.headers.get('cache-control') == 'no-store'


async def test_public_boundary_rejects_duplicate_origin_before_auth(pg_db):
    async with gateway_environment(pg_db) as env:
        response = await env.client.get('/health', headers=[
            ('origin', 'https://attacker.invalid'), ('origin', 'http://127.0.0.1:8788')])
        assert response.status_code == 403
        assert response.headers.get('cache-control') == 'no-store'


@pytest.mark.parametrize('operation', ['compact', 'websocket'])
async def test_image_only_model_does_not_gain_transport_capabilities(pg_db, operation):
    from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec
    async with codex_environment(pg_db, count=1) as env:
        await env.engine.catalog.put_binding(replace(env.bindings[0], upstream_model='gpt-image-2',
            capabilities=frozenset({'text', 'vision', 'images'})))
        request = ResponsesCodec().decode(BODY, {}).model_copy(update={'operation': operation})
        with pytest.raises(GatewayError, match='unsupported_feature'):
            prepared = await env.engine.prepare(env.principal, request, env.engine.meta(BODY))
            await prepared.close()
        assert not env.upstream_requests
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT count(*) FROM requests')) == 0


async def test_late_401_cannot_expire_newer_refreshed_access_token(pg_db):
    import hashlib
    import json
    async with codex_environment(pg_db, count=1) as env:
        credential = env.bindings[0].credential_id
        cipher = env.engine.vault.seal('credential', credential, json.dumps({
            'access_token': 'new-access', 'refresh_token': 'new-refresh', 'id_token': 'new-id'}).encode()).to_dict()
        async with pg_db.sessions.begin() as session:
            await session.execute(text('UPDATE credentials SET encrypted_secret=CAST(:cipher AS jsonb),'
                'token_generation=token_generation+1 WHERE id=:id'), {'id': credential, 'cipher': json.dumps(cipher)})
        await env.engine.credentials.expire_rejected_access(credential, hashlib.sha256(b'synthetic-access').digest())
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT token_expires_at>now() FROM credentials WHERE id=:id'), {'id': credential})


async def test_websocket_401_also_fences_the_rejected_token(pg_db):
    from tests.gateway.websocket_support import SocketTransport, asgi_socket
    from .test_codex_websocket import CREATE, headers
    async with codex_environment(pg_db, count=1) as env:
        env.engine.transport.adapter = SocketTransport(status=401)
        async with asgi_socket(env.app, headers=headers(env)) as socket:
            await socket.receive()
            result = await socket.turn(CREATE)
            assert result[-1]['type'] == 'error'
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT token_expires_at<now() FROM credentials WHERE id=:id'),
                                        {'id': env.bindings[0].credential_id})
            assert await session.scalar(text('SELECT state FROM requests')) == 'released'


async def test_delayed_401_does_not_penalize_new_token_or_block_next_request(pg_db):
    import json
    async def respond(request):
        if request.headers['authorization'] == 'Bearer new-access':
            return httpx.Response(200, content=native_wire())
        # A refresh has committed while the old-token request was in flight.
        cipher = env.engine.vault.seal('credential', credential, json.dumps({
            'access_token': 'new-access', 'refresh_token': 'new-refresh', 'id_token': 'new-id'}).encode()).to_dict()
        async with pg_db.sessions.begin() as session:
            await session.execute(text('UPDATE credentials SET encrypted_secret=CAST(:cipher AS jsonb),'
                'token_generation=token_generation+1 WHERE id=:id'), {'id': credential, 'cipher': json.dumps(cipher)})
        return httpx.Response(401)

    async with codex_environment(pg_db, count=1, respond=respond) as env:
        credential = env.bindings[0].credential_id
        rejected = await env.client.post('/v1/responses', json=BODY, headers=env.headers)
        assert rejected.status_code == 502
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT cooldown_until FROM credentials WHERE id=:id'), {'id': credential}) is None
            assert await session.scalar(text('SELECT COALESCE((SELECT failure_count FROM codex_account_state '
                'WHERE credential_id=:id),0)'), {'id': credential}) == 0
        succeeded = await env.client.post('/v1/responses', json=BODY, headers=env.headers)
        assert succeeded.status_code == 200, succeeded.text
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT sum(amount_micro) FROM usage_ledger')) == 23


async def test_websocket_401_fails_over_before_generation_and_charges_once(pg_db):
    from tests.gateway.websocket_support import SocketTransport, asgi_socket
    from .test_codex_websocket import CREATE, headers, upstream_events

    class AccountHandshake(SocketTransport):
        async def handle_async_request(self, request):
            self.status = 401 if request.headers['chatgpt-account-id'] == 'account-0' else 101
            return await super().handle_async_request(request)

    async with codex_environment(pg_db, count=2) as env:
        adapter = AccountHandshake(upstream_events())
        env.engine.transport.adapter = adapter
        async with asgi_socket(env.app, headers=headers(env)) as socket:
            await socket.receive()
            result = await socket.turn(CREATE)
            assert result[-1]['type'] == 'response.completed'
        assert [request.headers['chatgpt-account-id'] for request in adapter.requests] == ['account-0', 'account-1']
        async with pg_db.sessions() as session:
            assert (await session.execute(text('SELECT status FROM attempts ORDER BY started_at'))).scalars().all() == ['rejected', 'completed']
            assert (await session.execute(text('SELECT count(*),sum(amount_micro) FROM usage_ledger'))).one() == (1, 20)
            assert await session.scalar(text('SELECT sum(held) FROM quota_buckets')) == 0


@pytest.mark.parametrize('metadata', [False, True, 'timing'])
async def test_websocket_rate_limit_advisory_is_not_a_response_lifecycle_event(pg_db, metadata):
    from tests.gateway.websocket_support import SocketTransport, asgi_socket
    from .test_codex_websocket import CREATE, headers, upstream_events
    # Real Codex sends this advisory before response.created. Its quota payload
    # is opaque here: only a completed response can provide billed token usage.
    advisory = {'type': 'codex.rate_limits', 'plan_type': 'plus', 'rate_limits': {},
                'code_review_rate_limits': {}, 'additional_rate_limits': [], 'credits': None, 'promo': None}
    if metadata is True:
        advisory = {'type': 'codex.response.metadata', 'metadata': {'conversation_id': 'test-conversation'}}
    elif metadata == 'timing':
        advisory = {'type': 'responsesapi.websocket_timing'}
    events = upstream_events()
    async with codex_environment(pg_db, count=1) as env:
        env.engine.transport.adapter = SocketTransport([advisory, events[0], advisory, *events[1:]])
        async with asgi_socket(env.app, headers=headers(env)) as socket:
            await socket.receive()
            result = await socket.turn(CREATE)
            assert result[-1]['type'] == 'response.completed'
            assert all(not event['type'].startswith('codex.') for event in result)
            assert result[-1]['response']['usage']['input_tokens'] == 4
        async with pg_db.sessions() as session:
            assert (await session.execute(text('SELECT count(*),sum(amount_micro) FROM usage_ledger'))).one() == (1, 20)
            assert await session.scalar(text('SELECT status FROM attempts')) == 'completed'
            assert await session.scalar(text('SELECT sum(held) FROM quota_buckets')) == 0


@pytest.mark.parametrize('status', [400, 401, 429])
async def test_websocket_explicit_bootstrap_rejection_fails_over_without_pending_hold(pg_db, status):
    from tests.gateway.websocket_support import SocketTransport, asgi_socket
    from .test_codex_websocket import CREATE, headers, upstream_events

    class BootstrapRejection(SocketTransport):
        async def handle_async_request(self, request):
            self.events = ([{'type': 'codex.rate_limits'}, {'type': 'error', 'status': status,
                'error': {'type': 'invalid_request_error', 'message': 'This model is not supported'}}]
                if request.headers['chatgpt-account-id'] == 'account-0' else upstream_events())
            return await super().handle_async_request(request)

    async with codex_environment(pg_db, count=2) as env:
        adapter = BootstrapRejection()
        env.engine.transport.adapter = adapter
        async with asgi_socket(env.app, headers=headers(env)) as socket:
            await socket.receive()
            result = await socket.turn(CREATE)
            assert result[-1]['type'] == 'response.completed'
        assert len(adapter.requests) == 2 and all(stream.closed for stream in adapter.streams)
        async with pg_db.sessions() as session:
            assert (await session.execute(text('SELECT status FROM attempts ORDER BY started_at'))).scalars().all() == ['rejected', 'completed']
            assert (await session.execute(text('SELECT count(*),sum(amount_micro) FROM usage_ledger'))).one() == (1, 20)
            assert await session.scalar(text('SELECT sum(held) FROM quota_buckets')) == 0


async def test_websocket_error_after_response_created_never_replays(pg_db):
    from tests.gateway.websocket_support import SocketTransport, asgi_socket
    from .test_codex_websocket import CREATE, headers, upstream_events
    async with codex_environment(pg_db, count=2) as env:
        adapter = SocketTransport([upstream_events()[0], {'type': 'error', 'status': 400,
            'error': {'message': 'This model is not supported'}}])
        env.engine.transport.adapter = adapter
        async with asgi_socket(env.app, headers=headers(env)) as socket:
            await socket.receive()
            result = await socket.turn(CREATE)
            assert result[-1]['type'] == 'error'
        assert len(adapter.requests) == 1
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT state FROM requests')) == 'usage_pending'
            assert await session.scalar(text('SELECT count(*) FROM usage_ledger')) == 0
