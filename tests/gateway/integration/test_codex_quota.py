import asyncio
from datetime import datetime, timedelta, timezone
import time

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from autobuild_json.gateway.errors import GatewayError
from .test_oauth_import import credential_service, record

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def deadline(seconds=25):
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


async def setup(db, mode='direct', *, override=None, active=True):
    from autobuild_json.gateway.accounts.quota import CodexQuotaService
    from autobuild_json.gateway.accounts.quota_store import QuotaStore
    from autobuild_json.gateway.proxy.profiles import ProfileStore
    from autobuild_json.gateway.transport.http import Transport
    from autobuild_json.gateway.transport.egress import EgressPolicy
    from types import SimpleNamespace
    calls, refresh_routes = [], []
    async def exchange(tokens, lease, end):
        refresh_routes.append(lease.proxy.server if lease.proxy else None)
        return {'id_token': 'fresh-id', 'access_token': 'fresh-access', 'refresh_token': 'fresh-refresh', 'expires_in': 3600}
    async def verify(*args):
        pass
    credentials = await credential_service(db, exchange, verify)
    profiles = ProfileStore(db, credentials.vault, b'p'*32)
    credentials.profile_resolver = profiles.load
    async def profile(kind):
        value = {'direct': '', 'fixed': 'http://fixed.invalid:8080', 'pool': 'http://pool.invalid:8080', 'kiotproxy': 'synthetic-kiot'}[kind]
        return await profiles.create(SimpleNamespace(name=kind, mode=kind, entries_text=SecretStr(value), region='random', protocol='http', rotate=False))
    provider_profile = await profile(mode)
    explicit = await profile(override) if override else None
    identity = await credentials.import_record(record(), explicit)
    async with db.sessions.begin() as session:
        await session.execute(text("UPDATE providers SET config=jsonb_set(config,'{proxy_profile_id}',to_jsonb(CAST(:p AS text))),version=version+1"), {'p': str(provider_profile)})
        if active:
            await session.execute(text("UPDATE credentials SET health='active',token_expires_at=now()+interval '1 hour'"))
    from autobuild_json.gateway.proxy.kiot import KiotClient
    kiot_calls = []
    def kiot_reply(request):
        kiot_calls.append(request)
        return httpx.Response(200, json={'success': True, 'data': {'http': '93.184.216.34:8080', 'ttl': 300, 'ttc': 0,
                                                                    'expirationAt': int(time.time()*1000)+300000, 'nextRequestAt': int(time.time()*1000)}, 'timestamp': int(time.time()*1000)})
    async def resolve(host, port):
        return ['93.184.216.34']
    policy = EgressPolicy(resolver=resolve)
    credentials.proxies.kiot = KiotClient(adapter=httpx.MockTransport(kiot_reply))
    payloads = {'usage': {'plan_type': 'plus', 'rate_limit': {'primary_window': {'used_percent': 25}}},
                'rate-limit-reset-credits': {'available_count': 2}}
    async def respond(request):
        calls.append(request)
        payload = payloads[request.url.path.rsplit('/', 1)[-1]]
        if callable(payload):
            return await payload(request)
        return httpx.Response(payload if type(payload) is int else 200, json=payload)
    routes = []
    class RecordingTransport(Transport):
        async def _validate(self, route, proxy):
            routes.append(proxy.server if proxy else None)
            await super()._validate(route, proxy)
    transport = RecordingTransport(policy, adapter=httpx.MockTransport(respond))
    service = CodexQuotaService(db, credentials, credentials.proxies, profiles, transport, QuotaStore(db))
    return SimpleNamespace(service=service, credentials=credentials, identity=identity, calls=calls, payloads=payloads,
                           routes=routes, refresh_routes=refresh_routes, kiot_calls=kiot_calls, profiles=profiles, profile_id=provider_profile)


async def no_leases(db):
    async with db.sessions() as session:
        assert await session.scalar(text('SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL')) == 0


@pytest.mark.parametrize('mode,override,want', [('direct', None, None), ('fixed', None, 'http://fixed.invalid:8080'),
    ('pool', None, 'http://pool.invalid:8080'), ('kiotproxy', None, 'http://93.184.216.34:8080'),
    ('kiotproxy', 'fixed', 'http://fixed.invalid:8080'), ('kiotproxy', 'direct', None)])
async def test_read_refresh_uses_effective_proxy_and_get_is_storage_only(pg_db, mode, override, want):
    env = await setup(pg_db, mode, override=override, active=False)
    assert await env.service.get(env.identity) is None
    assert await env.service.get_credits(env.identity) is None
    assert not env.calls and not env.kiot_calls and not env.refresh_routes
    result = await env.service.refresh(env.identity, deadline())
    assert result.primary.used_percent == 25
    assert (await env.service.get_credits(env.identity)).available_count == 2
    assert env.refresh_routes == [want]
    assert env.routes == [want, want]
    assert all(r.headers['authorization'] == 'Bearer fresh-access' for r in env.calls)
    assert all(r.headers['chatgpt-account-id'] == 'account-test' for r in env.calls)
    await env.service.get(env.identity)
    await env.service.get_credits(env.identity)
    assert len(env.calls) == 2
    await no_leases(pg_db)


async def test_401_refreshes_once_outside_exclusive_lease(pg_db):
    env = await setup(pg_db, 'fixed')
    async def unauthorized_until_refreshed(request):
        if request.headers['authorization'] == 'Bearer synthetic-access':
            return httpx.Response(401, json={'error': 'private-do-not-store'})
        return httpx.Response(200, json={'plan_type': 'plus'})
    env.payloads['usage'] = unauthorized_until_refreshed
    result = await env.service.refresh(env.identity, deadline())
    assert result.plan_type == 'plus'
    assert env.refresh_routes == ['http://fixed.invalid:8080']
    assert [r.url.path.rsplit('/', 1)[-1] for r in env.calls] == ['usage', 'usage', 'rate-limit-reset-credits']
    await no_leases(pg_db)


async def test_credit_401_retains_usage_if_oauth_refresh_fails(pg_db):
    env = await setup(pg_db)
    env.payloads['rate-limit-reset-credits'] = 401
    async def failed(*args):
        raise GatewayError('reauth_required', 401)
    env.credentials.exchange = failed
    with pytest.raises(GatewayError, match='reauth_required'):
        await env.service.refresh(env.identity, deadline())
    assert (await env.service.get(env.identity)).plan_type == 'plus'
    async with pg_db.sessions() as session:
        assert await session.scalar(text('SELECT credits_error FROM codex_account_state')) == 'codex_credits_unavailable'


async def test_401_does_not_expire_newer_concurrent_token(pg_db):
    env = await setup(pg_db)
    async def newer(request):
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE credentials SET token_generation=token_generation+1,token_expires_at=now()+interval '2 hours'"))
        return httpx.Response(401)
    env.payloads['usage'] = newer
    with pytest.raises(GatewayError, match='claim_lost'):
        await env.service.refresh(env.identity, deadline())
    assert not env.refresh_routes
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT token_expires_at > now()+interval '1 hour' FROM credentials"))


async def test_401_retry_is_bounded_once(pg_db):
    env = await setup(pg_db)
    env.payloads['usage'] = 401
    with pytest.raises(GatewayError, match='codex_usage_unavailable'):
        await env.service.refresh(env.identity, deadline())
    assert len(env.refresh_routes) == 1
    assert len(env.calls) == 3


async def test_real_oauth_http_and_jwt_verification_use_inherited_proxy(pg_db, monkeypatch):
    import json
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from curl_cffi import CurlOpt
    from autobuild_json.gateway.accounts.refresh import exchange_tokens, verify_tokens
    from autobuild_json import transport as legacy_transport
    env = await setup(pg_db, 'fixed', active=False)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid='quota-fixture', alg='RS256', use='sig')
    claims = {'iss': 'https://auth.openai.com', 'aud': 'app_EMoamEEZ73f0CkXaXp7hrann', 'iat': int(time.time()),
              'exp': int(time.time())+3600, 'sub': 'fixture', 'email': 'synthetic@example.com', 'email_verified': True,
              'https://api.openai.com/auth': {'chatgpt_account_id': 'account-test'}}
    token = jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'quota-fixture'})
    http_calls, closed = [], []
    class RawSession:
        def __init__(self, **kwargs):
            self.cookies, self.curl_options = {}, {}
        def request(self, **kwargs):
            http_calls.append(kwargs)
            assert self.curl_options[CurlOpt.PROXY] == 'http://fixed.invalid:8080'
            assert kwargs['allow_redirects'] is False
            payload = {'keys': [jwk]} if kwargs['method'] == 'GET' else {
                'id_token': token, 'access_token': 'real-verified-access', 'refresh_token': 'real-refresh', 'expires_in': 3600}
            from tests.fakes import Response
            body = json.dumps(payload).encode()
            kwargs['content_callback'](body)
            return Response(payload)
        def close(self):
            closed.append(True)
    monkeypatch.setattr(legacy_transport.requests, 'Session', RawSession)
    env.credentials.exchange, env.credentials.verifier = exchange_tokens, verify_tokens
    assert (await env.service.refresh(env.identity, deadline())).plan_type == 'plus'
    assert [r['method'] for r in http_calls] == ['POST', 'GET']
    assert http_calls[0]['url'] == 'https://auth.openai.com/oauth/token'
    assert len(closed) == 2
    assert all(r.headers['authorization'] == 'Bearer real-verified-access' for r in env.calls)
    await no_leases(pg_db)


async def test_policy_change_during_token_refresh_fences_quota_dispatch(pg_db):
    env = await setup(pg_db, 'fixed', active=False)
    exchange = env.credentials.exchange
    async def changed(*args):
        result = await exchange(*args)
        async with pg_db.sessions.begin() as session:
            await session.execute(text('UPDATE proxy_profiles SET version=version+1'))
        return result
    env.credentials.exchange = changed
    with pytest.raises(GatewayError, match='claim_lost'):
        await env.service.refresh(env.identity, deadline())
    assert not env.calls
    await no_leases(pg_db)


@pytest.mark.parametrize('change', ["UPDATE credentials SET token_generation=token_generation+1",
    "UPDATE credentials SET version=version+1", "UPDATE providers SET version=version+1",
    "UPDATE proxy_profiles SET version=version+1"])
async def test_old_dependency_snapshot_never_dispatches_second_get_or_persists(pg_db, change):
    env = await setup(pg_db, 'fixed')
    async def changed(request):
        async with pg_db.sessions.begin() as session:
            await session.execute(text(change))
        return httpx.Response(200, json={'plan_type': 'plus'})
    env.payloads['usage'] = changed
    with pytest.raises(GatewayError, match='claim_lost'):
        await env.service.refresh(env.identity, deadline())
    assert len(env.calls) == 1
    assert await env.service.get(env.identity) is None
    await no_leases(pg_db)


async def test_change_after_last_get_fences_save(pg_db):
    env = await setup(pg_db, 'fixed')
    async def changed(request):
        async with pg_db.sessions.begin() as session:
            await session.execute(text('UPDATE credentials SET token_generation=token_generation+1'))
        return httpx.Response(200, json={'available_count': 3})
    env.payloads['rate-limit-reset-credits'] = changed
    with pytest.raises(GatewayError, match='claim_lost'):
        await env.service.refresh(env.identity, deadline())
    assert len(env.calls) == 2
    assert await env.service.get_credits(env.identity) is None
    await no_leases(pg_db)


async def test_deadline_cancels_and_joins_http_before_lease_release(pg_db, monkeypatch):
    from types import SimpleNamespace
    from autobuild_json.gateway.accounts import quota as quota_module
    env = await setup(pg_db, 'fixed')
    closed, cancelled, held = [], [], []
    entered = asyncio.Event()
    original_wait_for = asyncio.wait_for
    owner = asyncio.current_task()
    async def deadline_at_http_entry(awaitable, timeout):
        if asyncio.current_task() is owner and timeout > 5:
            await original_wait_for(entered.wait(), 5)
            # The service's deadline expires only after this case reaches the
            # streaming boundary. No startup speed assumption is involved.
            awaitable.cancel()
            raise asyncio.TimeoutError
        return await original_wait_for(awaitable, timeout)
    monkeypatch.setattr(quota_module, "asyncio", SimpleNamespace(**{
        **vars(asyncio), "wait_for": deadline_at_http_entry}))
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            try:
                await asyncio.sleep(10)
                yield b'{}'
            finally:
                cancelled.append(True)
        async def aclose(self):
            async with pg_db.sessions() as session:
                held.append(await session.scalar(text('SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL')))
            closed.append(True)
    async def slow(request):
        return httpx.Response(200, stream=Slow())
    env.payloads['usage'] = slow
    started = time.monotonic()
    with pytest.raises(GatewayError, match='deadline_exceeded'):
        await env.service.refresh(env.identity, deadline(20))
    assert time.monotonic() - started < 5
    assert cancelled and closed and held == [1]
    assert len(env.calls) == 1
    await no_leases(pg_db)


async def test_deadline_before_proxy_resolution_never_opens_http(pg_db, monkeypatch):
    from types import SimpleNamespace
    from autobuild_json.gateway.accounts import quota as quota_module
    env = await setup(pg_db, 'fixed')
    entered, joined = asyncio.Event(), asyncio.Event()
    original_wait_for = asyncio.wait_for
    owner = asyncio.current_task()
    async def blocked_policy(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            joined.set()
    async def deadline_at_policy(awaitable, timeout):
        if asyncio.current_task() is owner and timeout > 5:
            await original_wait_for(entered.wait(), 5)
            awaitable.cancel()
            raise asyncio.TimeoutError
        return await original_wait_for(awaitable, timeout)
    monkeypatch.setattr(env.service.resolver, "policy", blocked_policy)
    monkeypatch.setattr(quota_module, "asyncio", SimpleNamespace(**{
        **vars(asyncio), "wait_for": deadline_at_policy}))
    with pytest.raises(GatewayError, match="deadline_exceeded"):
        await env.service.refresh(env.identity, deadline(20))
    assert joined.is_set() and not env.calls
    await no_leases(pg_db)


async def test_external_cancellation_joins_http_and_releases_proxy(pg_db):
    env = await setup(pg_db, 'fixed')
    entered, ended = asyncio.Event(), asyncio.Event()
    async def blocked(request):
        entered.set()
        try:
            await asyncio.sleep(20)
        finally:
            ended.set()
    env.payloads['usage'] = blocked
    task = asyncio.create_task(env.service.refresh(env.identity, deadline()))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ended.is_set()
    await no_leases(pg_db)


async def test_deadline_includes_database_lock_wait(pg_db):
    env = await setup(pg_db)
    async with pg_db.sessions.begin() as session:
        await session.execute(text('SELECT id FROM providers FOR UPDATE'))
        with pytest.raises(GatewayError, match='deadline_exceeded'):
            await env.service.refresh(env.identity, deadline(.05))
    assert not env.calls
    await no_leases(pg_db)


async def test_runtime_without_asyncio_timeout_and_repeated_cancellation(pg_db, monkeypatch):
    env = await setup(pg_db, 'fixed')
    monkeypatch.delattr(asyncio, 'timeout', raising=False)
    entered, closing, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Waiting(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.sleep(20)
            yield b'{}'
        async def aclose(self):
            closing.set()
            await finish.wait()
    async def waiting(request):
        return httpx.Response(200, stream=Waiting())
    env.payloads['usage'] = waiting
    task = asyncio.create_task(env.service.refresh(env.identity, deadline()))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    await asyncio.wait_for(closing.wait(), 2)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await no_leases(pg_db)


async def test_partial_failure_preserves_prior_success_and_safe_error(pg_db):
    env = await setup(pg_db)
    first = await env.service.refresh(env.identity, deadline())
    env.payloads['usage'] = {'rate_limit': {'primary_window': {'used_percent': 'secret-bad'}}}
    env.payloads['rate-limit-reset-credits'] = {'available_count': 3}
    result = await env.service.refresh(env.identity, deadline())
    assert result.fetched_at == first.fetched_at and result.primary == first.primary
    assert result.last_error == 'codex_usage_unavailable'
    assert (await env.service.get_credits(env.identity)).available_count == 3
    env.payloads['usage'] = {'plan_type': 'free'}
    env.payloads['rate-limit-reset-credits'] = 500
    result = await env.service.refresh(env.identity, deadline())
    assert result.plan_type == 'free' and result.last_error is None
    async with pg_db.sessions() as session:
        row = (await session.execute(text('SELECT usage_error,credits_error FROM codex_account_state'))).one()
    assert row == (None, 'codex_credits_unavailable')


async def test_uncertain_health_never_reactivated_by_quota(pg_db):
    env = await setup(pg_db)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE credentials SET health='refresh_uncertain'"))
    with pytest.raises(GatewayError, match='refresh_uncertain'):
        await env.service.refresh(env.identity, deadline())
    assert not env.calls


async def test_configured_proxy_failure_does_not_fall_back_direct(pg_db):
    env = await setup(pg_db, 'fixed')
    from uuid import uuid4
    from autobuild_json.gateway.proxy.manager import endpoint_resource
    from autobuild_json.models import ProxyConfig
    env.credentials.proxies.acquisition_timeout = 0.02
    token = await env.credentials.proxies.store.claim(endpoint_resource(ProxyConfig('http://fixed.invalid:8080')), uuid4(), deadline())
    try:
        with pytest.raises(GatewayError, match='proxy_not_ready'):
            await env.service.refresh(env.identity, deadline())
        assert not env.calls
    finally:
        await env.credentials.proxies.store.release(token)
    await no_leases(pg_db)
