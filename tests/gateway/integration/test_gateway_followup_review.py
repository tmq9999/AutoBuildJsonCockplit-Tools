"""Follow-up review: exercise real routes, policy and PostgreSQL accounting."""
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from autobuild_json.gateway.routing.records import ProviderConfig
from tests.gateway.harness import gateway_environment
from .test_anthropic_gateway import enable_anthropic
from .test_gemini_gateway import enable_gemini

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.mark.parametrize('body', [
    {'response_format': []}, {'response_format': {}},
    {'image': 'https://[broken-host/image.png'},
])
async def test_invalid_image_options_are_400_without_reservation(pg_db, body):
    async with gateway_environment(pg_db) as env:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app, raise_app_exceptions=False),
                                     base_url='http://127.0.0.1:8788') as client:
            result = await client.post('/v1/images/edits', json={
                'model': 'public', 'prompt': 'Use blue', 'image': 'data:image/png;base64,UE5H', **body},
                headers={'Authorization': 'Bearer '+env.secret})
        assert result.status_code == 400
        assert result.json()['error']['code'] == 'invalid_request'
        assert not env.upstream_requests
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT count(*) FROM requests')) == 0


@pytest.mark.parametrize('payload', [None, [], 'not-a-count', 7])
async def test_malformed_gemini_counter_is_safe_upstream_error(pg_db, payload):
    import json
    async with gateway_environment(pg_db, respond=lambda _: httpx.Response(200, content=json.dumps(payload))) as env:
        headers = await enable_gemini(env)
        await env.engine.catalog.update_provider(env.provider_id, 1, ProviderConfig(
            name='Counter', adapter='gemini', root='https://provider.invalid/v1beta', auth_mode='x-goog-api-key'))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app, raise_app_exceptions=False),
                                     base_url='http://127.0.0.1:8788') as client:
            result = await client.post('/v1beta/models/public:countTokens', json={'contents': []}, headers=headers)
        assert result.status_code == 502
        assert result.json() == {'error': {'code': 502, 'message': 'upstream_error', 'status': 'INTERNAL'}}
        assert len(env.upstream_requests) == 1


async def test_token_counter_rechecks_provider_after_dispatch_lock_wait(pg_db, monkeypatch):
    async with gateway_environment(pg_db, respond=lambda _: httpx.Response(200, json={'input_tokens': 17})) as env:
        headers = await enable_anthropic(env)
        provider = ProviderConfig(name='Counter', adapter='anthropic', root='https://provider.invalid/v1')
        await env.engine.catalog.update_provider(env.provider_id, 1, provider)
        original = env.ledger.mark_dispatched

        async def disable_after_dispatch(*args, **kwargs):
            await original(*args, **kwargs)
            await env.engine.catalog.update_provider(env.provider_id, 2, provider.model_copy(update={'enabled': False}))

        monkeypatch.setattr(env.ledger, 'mark_dispatched', disable_after_dispatch)
        result = await env.client.post('/v1/messages/count_tokens', json={'model': 'public', 'messages': []}, headers=headers)
        assert result.status_code >= 400
        assert not env.upstream_requests
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT state FROM requests')) == 'released'
            assert await session.scalar(text('SELECT status FROM attempts')) == 'rejected'
            assert await session.scalar(text('SELECT count(*) FROM provider_admissions WHERE active')) == 0


@pytest.mark.parametrize('path,expected', [
    ('/v1/models', {'error': {'code': 'storage_unavailable', 'message': 'storage_unavailable', 'stage': 'storage'}}),
    ('/v1/messages/count_tokens', {'type': 'error', 'error': {'type': 'api_error', 'message': 'storage_unavailable'}}),
    ('/v1beta/models', {'error': {'code': 503, 'message': 'storage_unavailable', 'status': 'UNAVAILABLE'}}),
    ('/api/tags', {'error': 'storage_unavailable'}),
])
async def test_storage_error_uses_client_protocol_without_private_details(pg_db, monkeypatch, path, expected):
    from autobuild_json.gateway.identity.service import IdentityService

    async def unavailable(*args):
        raise SQLAlchemyError('DO-NOT-ECHO database credentials')

    async with gateway_environment(pg_db) as env:
        monkeypatch.setattr(IdentityService, 'authenticate', unavailable)
        method = 'POST' if path.endswith('count_tokens') else 'GET'
        result = await env.client.request(method, path, json={} if method == 'POST' else None,
            headers={'Authorization': 'Bearer '+env.secret})
        assert result.status_code == 503
        assert result.json() == expected
        assert result.headers['cache-control'] == 'no-store'
