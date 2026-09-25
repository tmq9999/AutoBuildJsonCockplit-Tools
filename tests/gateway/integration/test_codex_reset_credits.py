"""Real storage, proxy leases and transport; only external HTTP is synthetic."""
import asyncio
import json
import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from autobuild_json.gateway.errors import GatewayError
from .test_codex_quota import setup as quota_setup, deadline, no_leases
from .test_codex_quota_store import snapshots

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def setup(db, mode='fixed'):
    from autobuild_json.gateway.accounts.reset_credits import ResetCreditService
    env = await quota_setup(db, mode)
    env.reset = ResetCreditService(env.service)
    claim = await env.service.store.begin_refresh(env.identity, deadline())
    assert await env.service.store.save_refresh(claim, *snapshots(env.identity), ())
    env.credits = await env.service.get_credits(env.identity)
    env.payloads['consume'] = {}
    return env


async def consume(env, request_id=None, **kwargs):
    return await env.reset.consume(env.identity, request_id or uuid4(),
        kwargs.pop('version', env.credits.version), kwargs.pop('acknowledge', True),
        'admin', kwargs.pop('end', deadline()), **kwargs)


def posts(env):
    return [r for r in env.calls if r.method == 'POST']


async def test_same_confirmation_and_reconnect_send_exactly_one_guarded_post(pg_db):
    env = await setup(pg_db)
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(request):
        entered.set()
        await release.wait()
        return httpx.Response(204)
    env.payloads['consume'] = blocked
    identity = uuid4()
    first = asyncio.create_task(consume(env, identity))
    await asyncio.wait_for(entered.wait(), 2)
    replay = await consume(env, identity)
    assert replay.state == 'dispatched'
    with pytest.raises(GatewayError, match='operation_conflict'):
        await consume(env)
    release.set()
    result = await first
    assert result.state == 'succeeded' and result.upstream_status == 204
    assert await consume(env, identity) == result
    assert len(posts(env)) == 1
    request = posts(env)[0]
    assert str(request.url) == 'https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume'
    assert request.headers['authorization'] == 'Bearer synthetic-access'
    assert request.headers['chatgpt-account-id'] == 'account-test'
    assert 'cookie' not in request.headers and not request.url.query
    async with pg_db.sessions() as session:
        row = (await session.execute(text('SELECT * FROM codex_reset_requests'))).mappings().one()
        assert json.loads(request.content) == {'redeem_request_id': str(row['redeem_request_id'])}
        assert row['redeem_request_id'] != identity
        actions = (await session.execute(text("SELECT action FROM audit_events WHERE action LIKE 'codex.reset.%' ORDER BY created_at"))).scalars().all()
        assert actions == ['codex.reset.prepared', 'codex.reset.dispatched', 'codex.reset.succeeded_refresh_failed', 'codex.reset.succeeded']
        assert await session.scalar(text('SELECT count(*) FROM quota_buckets')) == 0
    await no_leases(pg_db)


@pytest.mark.parametrize('kind', ['version', 'account', 'ack'])
async def test_replay_payload_mismatch_is_storage_only(pg_db, kind):
    env = await setup(pg_db, 'kiotproxy')
    result = await consume(env)
    # Any attempted token/proxy work after replay would now fail or allocate.
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE credentials SET health='reauth_required',enabled=false"))
    before = len(env.kiot_calls)
    assert await consume(env, result.operation_id) == result
    with pytest.raises(GatewayError, match='payload_mismatch') as caught:
        await env.reset.consume(uuid4() if kind == 'account' else env.identity,
            result.operation_id, env.credits.version + (kind == 'version'), kind != 'ack', 'admin', deadline())
    assert caught.value.status == 409
    assert len(posts(env)) == 1 and len(env.kiot_calls) == before


@pytest.mark.parametrize('count,version,ack,code', [(0, 0, True, 'codex_no_credits'),
    (None, 0, True, 'codex_no_credits'), (2, 1, True, 'version_conflict'), (2, 0, False, 'invalid_request')])
async def test_ineligible_confirmation_never_refreshes_tokens_or_allocates(pg_db, count, version, ack, code):
    env = await setup(pg_db, 'kiotproxy')
    claim = await env.service.store.begin_refresh(env.identity, deadline())
    assert await env.service.store.save_refresh(claim, *snapshots(env.identity, count=count), ())
    env.credits = await env.service.get_credits(env.identity)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE credentials SET token_expires_at=now()-interval '1 second'"))
    with pytest.raises(GatewayError, match=code):
        await consume(env, version=env.credits.version + version, acknowledge=ack)
    assert not env.calls and not env.kiot_calls and not env.refresh_routes


async def failed_credits_refresh(env, db):
    env.payloads['rate-limit-reset-credits'] = 500
    usage = await env.service.refresh(env.identity, deadline())
    assert usage.primary.used_percent == 25 and usage.last_error is None
    # A partial refresh retains the recent, positive snapshot and its old version.
    assert await env.service.get_credits(env.identity) == env.credits
    assert env.credits.available_count == 2
    async with db.sessions() as session:
        row = (await session.execute(text('SELECT credits_error,credits_fetched_at,version, '
            "credits_fetched_at BETWEEN clock_timestamp()-interval '120 seconds' AND clock_timestamp() AS recent "
            'FROM codex_account_state WHERE credential_id=:id'), {'id': env.identity})).mappings().one()
    assert row['credits_error'] == 'codex_credits_unavailable'
    assert row['credits_fetched_at'] == env.credits.fetched_at and row['recent']
    assert row['version'] > env.credits.version


async def test_failed_credits_refresh_rejects_old_confirmation_before_any_io(pg_db):
    env = await setup(pg_db, 'kiotproxy')
    await failed_credits_refresh(env, pg_db)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE credentials SET token_expires_at=now()-interval '1 second'"))
    before = (len(env.calls), len(env.kiot_calls), len(env.refresh_routes))
    with pytest.raises(GatewayError, match='codex_credits_stale') as caught:
        await consume(env, version=env.credits.version)
    assert caught.value.status == 409
    assert (len(env.calls), len(env.kiot_calls), len(env.refresh_routes)) == before
    assert not posts(env)
    async with pg_db.sessions() as session:
        assert await session.scalar(text('SELECT count(*) FROM codex_reset_requests')) == 0
    await no_leases(pg_db)


async def test_failed_credits_refresh_after_preflight_is_rechecked_at_prepare(pg_db, monkeypatch):
    env = await setup(pg_db, 'kiotproxy')
    preflight = env.service.store.preflight_reset
    before = []
    async def refresh_after_preflight(*args):
        await preflight(*args)
        await failed_credits_refresh(env, pg_db)
        before.append((len(env.calls), len(env.kiot_calls), len(env.refresh_routes)))
    monkeypatch.setattr(env.service.store, 'preflight_reset', refresh_after_preflight)
    with pytest.raises(GatewayError, match='codex_credits_stale') as caught:
        await consume(env, version=env.credits.version)
    assert caught.value.status == 409
    assert before == [(len(env.calls), len(env.kiot_calls), len(env.refresh_routes))]
    assert not posts(env)
    async with pg_db.sessions() as session:
        assert await session.scalar(text('SELECT count(*) FROM codex_reset_requests')) == 0
    await no_leases(pg_db)


async def test_failed_credits_refresh_is_rechecked_at_dispatch_and_success_recovers(pg_db):
    env = await setup(pg_db, 'kiotproxy')
    claim = await env.service.store.prepare_reset(env.identity, uuid4(), env.credits.version, 'admin', deadline=deadline())
    env.payloads['rate-limit-reset-credits'] = 500
    await env.service.refresh(env.identity, deadline())
    assert not await env.service.store.mark_dispatched(claim.result.operation_id, claim.generation)
    assert not [request for request in env.calls if request.url.path.endswith('/consume')]

    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE codex_reset_requests SET requested_at=clock_timestamp()-interval '2 minutes', "
                                   "deadline=clock_timestamp()-interval '1 second'"))
    assert await env.service.store.recover_expired() == 1
    env.payloads['rate-limit-reset-credits'] = {'available_count': 2}
    await env.service.refresh(env.identity, deadline())
    env.credits = await env.service.get_credits(env.identity)
    await env.service.store.preflight_reset(env.identity, env.credits.version)
    await no_leases(pg_db)


@pytest.mark.parametrize('status,state', [(400, 'rejected'), (401, 'rejected'), (403, 'rejected'),
    (404, 'rejected'), (409, 'rejected'), (422, 'rejected'), (429, 'rejected'),
    (301, 'unknown'), (408, 'unknown'), (500, 'unknown'), (503, 'unknown')])
async def test_status_classification_never_reads_error_body_or_retries(pg_db, caplog, status, state):
    env = await setup(pg_db)
    async def rejected(request):
        return httpx.Response(status, content=b'sentinel-raw-private-response', headers={'Retry-After': '17'})
    env.payloads['consume'] = rejected
    result = await consume(env)
    assert result.state == state and result.upstream_status == status
    assert await consume(env, result.operation_id) == result
    assert len(posts(env)) == 1 and not env.refresh_routes and len(env.calls) == 1
    async with pg_db.sessions() as session:
        audits = (await session.execute(text("SELECT details FROM audit_events WHERE action LIKE 'codex.reset.%'"))).scalars().all()
    assert 'sentinel-raw-private-response' not in result.model_dump_json() + str(audits) + caplog.text
    if status == 429:
        assert any(a.get('retry_after') == 17 for a in audits)
        with pytest.raises(GatewayError, match='rate_limited'):
            await consume(env)
        assert len(posts(env)) == 1


@pytest.mark.parametrize('failure', ['usage', 'rate-limit-reset-credits'])
async def test_receipt_is_durable_before_refresh_and_partial_refresh_stays_locked(pg_db, failure):
    env = await setup(pg_db)
    async def failed(request):
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT state FROM codex_reset_requests')) == 'succeeded_refresh_failed'
        return httpx.Response(500)
    env.payloads[failure] = failed
    result = await consume(env)
    assert result.state == 'succeeded_refresh_failed'
    env.payloads[failure] = {}
    await env.service.refresh(env.identity, deadline())
    with pytest.raises(GatewayError, match='operation_conflict'):
        await consume(env)
    assert await consume(env, result.operation_id) == result
    assert len(posts(env)) == 1


async def test_dropped_response_is_unknown_and_resolution_never_changes_customer_ledger(pg_db):
    env = await setup(pg_db)
    async def dropped(request):
        raise httpx.ReadError('sentinel-private-drop')
    env.payloads['consume'] = dropped
    result = await consume(env)
    assert result.state == 'unknown'
    await env.service.refresh(env.identity, deadline())
    with pytest.raises(GatewayError, match='operation_conflict'):
        await consume(env)
    with pytest.raises(GatewayError, match='invalid_request'):
        await env.reset.resolve(result.operation_id, result.version, 'confirmed_applied', 'Bearer secret', 'admin')
    resolved = await env.reset.resolve(result.operation_id, result.version, 'confirmed_not_applied', 'Provider confirmed not applied', 'admin')
    assert resolved.state == 'rejected'
    assert await consume(env, result.operation_id) == resolved
    async with pg_db.sessions() as session:
        assert await session.scalar(text('SELECT credits_fetched_at FROM codex_account_state')) is None
        assert await session.scalar(text('SELECT count(*) FROM quota_buckets')) == 0
    assert len(posts(env)) == 1


async def test_token_refresh_happens_before_prepare(pg_db):
    env = await setup(pg_db)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE credentials SET token_expires_at=now()-interval '1 second'"))
    exchange = env.credentials.exchange
    async def refresh(*args):
        async with pg_db.sessions() as session:
            assert await session.scalar(text('SELECT count(*) FROM codex_reset_requests')) == 0
        return await exchange(*args)
    env.credentials.exchange = refresh
    assert (await consume(env)).state == 'succeeded'
    assert posts(env)[0].headers['authorization'] == 'Bearer fresh-access'
    assert len(env.refresh_routes) == 1


@pytest.mark.parametrize('mutation', ['UPDATE credentials SET token_generation=token_generation+1',
    'UPDATE providers SET version=version+1', 'UPDATE proxy_profiles SET version=version+1'])
async def test_changes_after_prepare_fail_dispatch_without_post(pg_db, mutation):
    env = await setup(pg_db)
    prepare = env.service.store.prepare_reset
    async def changed(*args, **kwargs):
        claim = await prepare(*args, **kwargs)
        async with pg_db.sessions.begin() as session:
            await session.execute(text(mutation))
        return claim
    env.service.store.prepare_reset = changed
    with pytest.raises(GatewayError, match='claim_lost'):
        await consume(env)
    assert not posts(env)
    await no_leases(pg_db)


async def test_deadline_dropped_post_recovers_unknown_without_retry(pg_db):
    env = await setup(pg_db)
    entered, expire, ended = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def blocked(request):
        entered.set()
        try:
            await expire.wait()
            raise httpx.ReadTimeout('synthetic timeout after dispatch')
        finally:
            ended.set()
    env.payloads['consume'] = blocked
    start = time.monotonic()
    task = asyncio.create_task(consume(env))
    await asyncio.wait_for(entered.wait(), 2)
    assert len(posts(env)) == 1
    # Expire the durable claim only after proving HTTP dispatch. This avoids
    # confusing slow DB setup (prepared expiry) with a dropped dispatched POST.
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE codex_reset_requests SET requested_at=now()-interval '2 minutes',deadline=now()-interval '1 minute'"))
    expire.set()
    result = await task
    assert time.monotonic() - start < 5
    assert result.state == 'unknown' and ended.is_set()
    assert await consume(env, result.operation_id) == result
    assert len(posts(env)) == 1
    await no_leases(pg_db)


async def test_repeated_cancellation_closes_receipt_before_releasing_proxy(pg_db, monkeypatch):
    monkeypatch.delattr(asyncio, 'timeout', raising=False)
    env = await setup(pg_db)
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Response(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b''
        async def aclose(self):
            entered.set()
            await release.wait()
            async with pg_db.sessions() as session:
                assert await session.scalar(text('SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL')) == 1
            closed.set()
    async def response(request):
        return httpx.Response(200, stream=Response())
    env.payloads['consume'] = response
    identity = uuid4()
    task = asyncio.create_task(consume(env, identity))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    result = await consume(env, identity)
    assert result.state == 'succeeded_refresh_failed' and len(posts(env)) == 1
    await no_leases(pg_db)


async def test_429_receipt_survives_close_failure_and_cooldown_is_maximum(pg_db):
    env = await setup(pg_db)
    class BrokenClose(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b''
        async def aclose(self):
            raise httpx.ReadError('sentinel-private-close')
    async def response(request):
        return httpx.Response(429, headers={'Retry-After': '17'}, stream=BrokenClose())
    env.payloads['consume'] = response
    result = await consume(env)
    assert result.state == 'rejected' and result.upstream_status == 429
    with pytest.raises(GatewayError, match='rate_limited'):
        await consume(env)
    assert len(posts(env)) == 1
    await no_leases(pg_db)


async def test_replay_storage_wait_obeys_total_deadline(pg_db):
    env = await setup(pg_db)
    # ACCESS EXCLUSIVE models a blocked read without replacing storage behavior.
    async with pg_db.sessions.begin() as blocker:
        await blocker.execute(text('LOCK TABLE codex_reset_requests IN ACCESS EXCLUSIVE MODE'))
        task = asyncio.create_task(consume(env, end=deadline(.05)))
        done, _ = await asyncio.wait({task}, timeout=.3)
        if not done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert done, 'storage replay escaped the total deadline'
        with pytest.raises(GatewayError, match='deadline_exceeded'):
            await task
    assert not env.calls


async def test_simultaneous_same_uuid_initial_reads_still_post_once(pg_db):
    env = await setup(pg_db)
    identity = uuid4()
    results = await asyncio.gather(*(consume(env, identity) for _ in range(5)))
    assert all(r.operation_id == identity for r in results)
    assert len(posts(env)) == 1
    assert (await consume(env, identity)).state == 'succeeded'


async def test_late_receipt_cannot_override_recovery_resolution_or_refresh(pg_db):
    env = await setup(pg_db)
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(request):
        entered.set()
        await release.wait()
        return httpx.Response(200)
    env.payloads['consume'] = blocked
    identity = uuid4()
    task = asyncio.create_task(consume(env, identity))
    await asyncio.wait_for(entered.wait(), 2)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE codex_reset_requests SET requested_at=now()-interval '2 minutes',deadline=now()-interval '1 minute'"))
    assert await env.service.store.recover_expired() == 1
    unknown = await env.service.store.get_reset(identity)
    resolved = await env.reset.resolve(identity, unknown.version, 'confirmed_not_applied', 'Verified provider evidence', 'admin')
    release.set()
    assert await task == resolved
    assert len(env.calls) == 1 and len(posts(env)) == 1
    await no_leases(pg_db)


async def test_external_cancellation_before_receipt_is_unknown_and_joined(pg_db):
    env = await setup(pg_db)
    entered, ended = asyncio.Event(), asyncio.Event()
    async def blocked(request):
        entered.set()
        try:
            await asyncio.sleep(20)
        finally:
            ended.set()
    env.payloads['consume'] = blocked
    identity = uuid4()
    task = asyncio.create_task(consume(env, identity))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ended.is_set()
    assert (await consume(env, identity)).state == 'unknown'
    assert len(posts(env)) == 1
    await no_leases(pg_db)


@pytest.mark.parametrize('outcome,state', [('confirmed_applied', 'succeeded'), ('confirmed_not_applied', 'rejected')])
async def test_explicit_resolution_requires_cas_printable_evidence(pg_db, outcome, state):
    env = await setup(pg_db)
    env.payloads['consume'] = 503
    result = await consume(env)
    with pytest.raises(GatewayError, match='version_conflict'):
        await env.reset.resolve(result.operation_id, result.version - 1, outcome, 'Verified evidence', 'admin')
    for reason in ('', 'x'*501, 'line\nbreak', 'https://private.invalid', 'access_token=value'):
        with pytest.raises(GatewayError, match='invalid_request'):
            await env.reset.resolve(result.operation_id, result.version, outcome, reason, 'admin')
    resolved = await env.reset.resolve(result.operation_id, result.version, outcome, 'Verified provider evidence', 'admin')
    assert resolved.state == state
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='codex.reset.resolved'")) == 1
        assert await session.scalar(text('SELECT count(*) FROM key_quota_adjustments')) == 0
        assert await session.scalar(text('SELECT count(*) FROM quota_buckets')) == 0
    assert (await env.service.get(env.identity)).primary.used_percent == 42
    assert len(posts(env)) == 1


@pytest.mark.parametrize('mutation', ['UPDATE credentials SET token_generation=token_generation+1',
                                    'UPDATE proxy_profiles SET version=version+1'])
async def test_inflight_dependency_change_retains_lock_until_unknown_recovery(pg_db, mutation):
    env = await setup(pg_db)
    async def changed(request):
        async with pg_db.sessions.begin() as session:
            await session.execute(text(mutation))
        return httpx.Response(200)
    env.payloads['consume'] = changed
    result = await consume(env)
    assert result.state == 'dispatched'
    with pytest.raises(GatewayError, match='operation_conflict'):
        await consume(env)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE codex_reset_requests SET requested_at=now()-interval '2 minutes',deadline=now()-interval '1 minute'"))
    from autobuild_json.gateway.maintenance import Maintenance
    assert await Maintenance(pg_db).tick() == 1
    assert (await consume(env, result.operation_id)).state == 'unknown'
    assert len(env.calls) == 1 and len(posts(env)) == 1


async def test_existing_cooldown_is_not_shortened_by_rejected_receipt(pg_db):
    env = await setup(pg_db)
    async def rejected(request):
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE credentials SET cooldown_until=now()+interval '1 hour'"))
        return httpx.Response(429, headers={'Retry-After': '1'})
    env.payloads['consume'] = rejected
    assert (await consume(env)).state == 'rejected'
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT cooldown_until > now()+interval '59 minutes' FROM credentials"))
    assert len(posts(env)) == 1
