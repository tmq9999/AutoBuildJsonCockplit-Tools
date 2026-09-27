"""Whole-branch regressions: wall deadlines, wait episodes, and serving state."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.providers.limits import ProviderLimits
from autobuild_json.gateway.proxy.pg_leases import PgLeaseStore
from autobuild_json.gateway.storage.db import make_database
from .test_provider_admission_wait import limited_route

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def future(seconds=30):
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


@asynccontextmanager
async def small_pool(pg_db, postgres_url):
    async with pg_db.sessions() as session:
        schema = await session.scalar(text("SELECT current_schema()"))
    db = make_database(SecretStr(postgres_url), schema=schema, pool_size=3, max_overflow=0)
    try:
        yield db
    finally:
        await db.close()


@pytest.mark.parametrize("kind", ["provider", "proxy"])
async def test_pool_checkout_obeys_acquisition_deadline(pg_db, postgres_url, kind):
    route, _, _ = await limited_route(pg_db)
    async with small_pool(pg_db, postgres_url) as db:
        async with AsyncExitStack() as blockers:
            for _ in range(3):
                await blockers.enter_async_context(db.engine.connect())
            if kind == "provider":
                context = ProviderLimits(db).acquire(route, future(.08), wait=True)
                operation = context.__aenter__()
            else:
                operation = PgLeaseStore(db).claim("pool-blocked", uuid4(), future(.08))
            pending = asyncio.create_task(operation)
            await asyncio.sleep(.2)
            completed_on_time = pending.done()
            if not pending.done():
                pending.cancel()
        result = (await asyncio.gather(pending, return_exceptions=True))[0]
        assert completed_on_time, "pool checkout escaped the request deadline"
        assert isinstance(result, GatewayError) and result.code == "deadline_exceeded"
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM provider_admissions")) == 0
        assert await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL")) == 0


@pytest.mark.parametrize("kind", ["provider", "proxy"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_cleanup_pool_checkout_has_independent_bounded_grace(pg_db, postgres_url, kind, cancel):
    route, _, _ = await limited_route(pg_db)
    async with small_pool(pg_db, postgres_url) as db:
        if kind == "provider":
            limits = ProviderLimits(db)
            context = limits.acquire(route, future())
            await context.__aenter__()
            operation = context.__aexit__(None, None, None)
        else:
            store = PgLeaseStore(db)
            token = await store.claim("cleanup-blocked", uuid4(), future())
            operation = store.release(token)
        async with AsyncExitStack() as blockers:
            for _ in range(3):
                await blockers.enter_async_context(db.engine.connect())
            pending = asyncio.create_task(operation)
            await asyncio.sleep(.02)
            if cancel:
                pending.cancel()
            await asyncio.sleep(limits._CLEANUP_RETRY_SECONDS + .1 if kind == "provider" else .4)
            completed_on_time = pending.done()
        result = (await asyncio.gather(pending, return_exceptions=True))[0]
        assert completed_on_time, "owned cleanup escaped its bounded retry window"
        if cancel:
            assert isinstance(result, (asyncio.CancelledError, GatewayError))
        elif kind == "provider":
            assert result is False
            assert limits.snapshot()["cleanup_failures"] == 1
        else:
            assert isinstance(result, GatewayError) and result.code == "deadline_exceeded"
        assert db.engine.pool.checkedout() == 0


async def test_provider_cleanup_retries_after_transient_pool_contention(pg_db, postgres_url):
    """A settled request must survive one pool-starved admission cleanup attempt."""
    route, _, _ = await limited_route(pg_db)
    async with small_pool(pg_db, postgres_url) as db:
        limits = ProviderLimits(db)
        context = limits.acquire(route, future())
        await context.__aenter__()
        blockers = [await db.engine.connect() for _ in range(3)]
        pending = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.sleep(limits._CLEANUP_GRACE_SECONDS + 0.05)
        await blockers[0].close()
        assert await asyncio.wait_for(pending, 1) is False
        for blocker in blockers[1:]:
            await blocker.close()
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0


async def test_same_provider_waiters_do_not_starve_pool_or_cleanup(pg_db, postgres_url):
    route, _, _ = await limited_route(pg_db, rpm=600)
    async with small_pool(pg_db, postgres_url) as db:
        limits = ProviderLimits(db)
        deadline = future(5)

        async def waiter():
            async with limits.acquire(route, deadline, wait=True):
                await asyncio.sleep(0)

        async with limits.acquire(route, deadline):
            pending = [asyncio.create_task(waiter()) for _ in range(100)]
            await asyncio.sleep(.1)
        results = await asyncio.gather(*pending, return_exceptions=True)

        assert results == [None] * 100
        assert limits.snapshot()["cleanup_failures"] == 0
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0


async def test_proxy_cleanup_retries_after_transient_pool_contention(pg_db, postgres_url):
    from autobuild_json.gateway.proxy.config import ProxySelection
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.pg_leases import PgLeaseStore
    from autobuild_json.models import ProxyConfig

    async with small_pool(pg_db, postgres_url) as db:
        manager = ProxyManager(
            PgLeaseStore(db), acquisition_timeout=.2, heartbeat_interval=10,
        )
        selection = ProxySelection("fixed", runtime_entries=(ProxyConfig("http://proxy.invalid:80"),))
        context = manager.acquire(selection, uuid4(), future())
        await context.__aenter__()
        blockers = [await db.engine.connect() for _ in range(3)]
        pending = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.sleep(.3)
        await blockers[0].close()
        assert await asyncio.wait_for(pending, 1) is False
        for blocker in blockers[1:]:
            await blocker.close()
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL")) == 0
        assert manager.snapshot()["cleanup_failures"] == 0


async def test_provider_lock_reservation_prevents_duplicate_same_key_lock(pg_db):
    limits = ProviderLimits(pg_db)
    limits._PROVIDER_LOCK_CAPACITY = 1
    anchor = limits._provider_lock("anchor")
    overflow = limits._provider_lock("overflow")
    assert limits._provider_overflow_mode
    limits._provider_unlock("anchor", anchor)
    assert limits._provider_lock("overflow") is overflow
    assert limits._provider_lock("future") is limits._provider_lock("future")


async def test_proxy_lock_reservation_prevents_duplicate_same_key_lock(pg_db):
    from autobuild_json.gateway.proxy.manager import ProxyManager

    manager = ProxyManager(PgLeaseStore(pg_db))
    manager._RESOURCE_LOCK_CAPACITY = 1
    anchor = manager._resource_lock("anchor")
    overflow = manager._resource_lock("overflow")
    assert manager._resource_overflow_mode
    manager._resource_unlock("anchor", anchor)
    assert manager._resource_lock("overflow") is overflow
    assert manager._resource_lock("future") is manager._resource_lock("future")


async def test_expired_proxy_claim_is_redacted_deadline_error(pg_db):
    with pytest.raises(GatewayError, match="deadline_exceeded"):
        await PgLeaseStore(pg_db).claim("expired", uuid4(), future(-1))


async def test_provider_sample_is_one_total_episode_not_poll_slices(pg_db):
    route, _, _ = await limited_route(pg_db)
    holder = ProviderLimits(pg_db)
    waiter = ProviderLimits(pg_db)
    async with holder.acquire(route, future()):
        context = waiter.acquire(route, future(), wait=True)
        pending = asyncio.create_task(context.__aenter__())
        await asyncio.sleep(.32)
    await pending
    await context.__aexit__(None, None, None)
    snapshot = waiter.snapshot()
    assert snapshot["wait_p50_ms"] >= 300
    assert len(waiter._waits) == 1
    assert snapshot["wait_attempts"] > 1


async def test_provider_then_proxy_waits_share_one_deadline_and_leave_no_rows(pg_db):
    from types import SimpleNamespace

    from autobuild_json.gateway.admission import InferenceAdmission
    from autobuild_json.gateway.engine import Engine
    from autobuild_json.gateway.proxy.config import ProxySelection
    from autobuild_json.gateway.proxy.manager import ProxyManager, endpoint_resource
    from autobuild_json.models import ProxyConfig

    route, _, _ = await limited_route(pg_db, rpm=600)
    limits = ProviderLimits(pg_db)
    store = PgLeaseStore(pg_db)
    manager = ProxyManager(store, acquisition_timeout=1, heartbeat_interval=.1)
    proxy = ProxyConfig("http://proxy.invalid:80")
    selection = ProxySelection("fixed", runtime_entries=(proxy,))
    provider_holder = limits.acquire(route, future(2))
    await provider_holder.__aenter__()
    proxy_holder = await store.claim(endpoint_resource(proxy), uuid4(), future(2))

    engine = Engine.__new__(Engine)
    engine.admission = InferenceAdmission(2)
    engine.provider_limits = limits
    engine.proxies = manager
    engine.transport = SimpleNamespace(_validate=lambda route, proxy: asyncio.sleep(0))
    deadline = future(.18)

    async def release_provider():
        await asyncio.sleep(.08)
        await provider_holder.__aexit__(None, None, None)

    release = asyncio.create_task(release_provider())
    try:
        with pytest.raises(GatewayError) as caught:
            async with engine.route_resources(route, selection, uuid4(), deadline):
                pytest.fail("busy proxy must consume the remaining request deadline")
        assert caught.value.code == "deadline_exceeded"
        assert caught.value.stage == "proxy"
        await release
        assert limits.snapshot()["wait_p50_ms"] >= 50
        assert manager.snapshot()["last_wait_ms"] > 0
        assert engine.admission.snapshot()["deadline_expired_by_stage"] == {
            "provider": 0, "proxy": 1, "upstream": 0,
        }
    finally:
        if not release.done():
            release.cancel()
            await asyncio.gather(release, return_exceptions=True)
        await store.release(proxy_holder)

    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0
        assert await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL")) == 0


async def test_cancelled_proxy_wait_releases_provider_and_proxy_rows(pg_db):
    from types import SimpleNamespace

    from autobuild_json.gateway.admission import InferenceAdmission
    from autobuild_json.gateway.engine import Engine
    from autobuild_json.gateway.proxy.config import ProxySelection
    from autobuild_json.gateway.proxy.manager import ProxyManager, endpoint_resource
    from autobuild_json.models import ProxyConfig

    route, _, _ = await limited_route(pg_db, rpm=600)
    limits = ProviderLimits(pg_db)
    store = PgLeaseStore(pg_db)
    manager = ProxyManager(store, acquisition_timeout=1, heartbeat_interval=.1)
    proxy = ProxyConfig("http://proxy.invalid:80")
    selection = ProxySelection("fixed", runtime_entries=(proxy,))
    held = await store.claim(endpoint_resource(proxy), uuid4(), future(2))
    engine = Engine.__new__(Engine)
    engine.admission = InferenceAdmission(2)
    engine.provider_limits = limits
    engine.proxies = manager
    engine.transport = SimpleNamespace(_validate=lambda route, proxy: asyncio.sleep(0))

    async def request():
        async with engine.route_resources(route, selection, uuid4(), future(2)):
            pytest.fail("busy proxy must keep the request waiting")

    pending = asyncio.create_task(request())
    while manager.snapshot()["wait_attempts"] == 0:
        await asyncio.sleep(.01)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await store.release(held)

    assert limits.snapshot()["cancelled"] == 0
    assert manager.snapshot()["cancelled"] == 1
    assert engine.admission.snapshot()["cancelled_by_stage"] == {
        "provider": 0, "proxy": 1, "upstream": 0,
    }
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0
        assert await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL")) == 0
