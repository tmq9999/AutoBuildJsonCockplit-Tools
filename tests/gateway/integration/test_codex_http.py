import pytest
from sqlalchemy import text

from tests.gateway.harness import gateway_environment

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('path', ['/v1/responses', '/backend-api/codex/responses', '/v1/chat/completions'])
async def test_supported_http_uses_existing_accounting(pg_db, path, stream):
    async with gateway_environment(pg_db) as env:
        body = {'model': 'public', 'stream': stream}
        body.update({'messages': [{'role': 'user', 'content': 'Hi'}]} if path.endswith('completions') else {'input': 'Hi'})
        result = await env.client.post(path, json=body, headers={'Authorization': 'Bearer '+env.secret})
        assert result.status_code == 200, result.text
        assert 'Hello' in result.text
        assert 'text/event-stream' in result.headers['content-type'] if stream else result.json()['model'] == 'public'
        assert len(env.upstream_requests) == 1
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT count(*) FROM usage_ledger')) == 1


@pytest.mark.parametrize('path', ['/v1/responses/compact', '/backend-api/codex/responses/compact',
                                  '/v1/images/generations', '/v1/images/edits'])
async def test_feature_http_authenticates_and_validates_without_reservation(pg_db, path):
    async with gateway_environment(pg_db) as env:
        unauth = await env.client.post(path, json={})
        assert unauth.status_code == 401
        result = await env.client.post(path, json={}, headers={'Authorization': 'Bearer '+env.secret})
        assert result.status_code == 400
        multipart = await env.client.post(path, files={'file': ('image.png', b'not-an-image')},
                                          headers={'Authorization': 'Bearer '+env.secret})
        assert multipart.status_code in ({400} if path.endswith('/images/edits') else {415})
        assert not env.upstream_requests
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT count(*) FROM requests')) == 0


async def test_backend_is_exact_and_unknown_options_remain_unsupported(pg_db):
    async with gateway_environment(pg_db) as env:
        headers = {'Authorization': 'Bearer '+env.secret}
        for path in ['/backend-api/codex/anything', '/backend-api/codex/responses/anything',
                     '/backend-api/codex/%2e%2e/anything', '/backend-api/codex/responses/']:
            result = await env.client.post(path, json={}, headers=headers)
            assert result.status_code == 404, (path, result.status_code)
        assert (await env.client.get('/backend-api/codex/responses', headers=headers)).status_code in {404, 405}
        result = await env.client.post('/backend-api/codex/responses', headers=headers,
                                       json={'model': 'public', 'input': 'Hi', 'native_codex_option': True})
        assert result.status_code == 400 and result.json()['error']['code'] == 'unsupported_feature'
        assert not env.upstream_requests


@pytest.mark.parametrize('path', ['/v1/responses', '/backend-api/codex/responses'])
async def test_websocket_closes_before_accept(pg_db, path):
    async with gateway_environment(pg_db) as env:
        sent = []
        async def receive():
            return {'type': 'websocket.connect'}
        async def send(message):
            sent.append(message)
        await env.app({'type': 'websocket', 'path': path, 'raw_path': path.encode(), 'scheme': 'ws',
                       'query_string': b'', 'headers': [], 'client': ('127.0.0.1', 123),
                       'server': ('127.0.0.1', 8788), 'root_path': '', 'subprotocols': []}, receive, send)
        assert sent and sent[-1]['type'] == 'websocket.close'
        assert sent[-1]['code'] in {1011, 1008}
        assert not env.upstream_requests
