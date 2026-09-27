import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from autobuild_json.gateway.errors import GatewayError

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_shared_key_lease_is_exclusive_and_fenced(pg_db):
    from autobuild_json.gateway.proxy.pg_leases import PgLeaseStore
    store = PgLeaseStore(pg_db)
    end = datetime.now(timezone.utc) + timedelta(seconds=60)
    first, second = await asyncio.gather(store.claim("key-fingerprint", uuid4(), end),
                                         store.claim("key-fingerprint", uuid4(), end))
    assert (first is None) != (second is None)
    held = first or second
    await store.release(held)
    new = await store.claim("key-fingerprint", uuid4(), end)
    assert new.generation > held.generation
    await store.release(held)
    assert await store.assert_owner(new)
    assert not await store.assert_owner(held)


async def test_lost_owner_grace_prevents_early_rotation(pg_db):
    from autobuild_json.gateway.proxy.pg_leases import PgLeaseStore
    now = datetime.now(timezone.utc)
    store = PgLeaseStore(pg_db, clock=lambda: now)
    held = await store.claim("key-fingerprint", uuid4(), now+timedelta(seconds=10))
    later = PgLeaseStore(pg_db, clock=lambda: now+timedelta(seconds=20))
    assert await later.claim("key-fingerprint", uuid4(), now+timedelta(seconds=60)) is None
    later = PgLeaseStore(pg_db, clock=lambda: now+timedelta(seconds=26))
    new = await later.claim("key-fingerprint", uuid4(), now+timedelta(seconds=60))
    assert new.generation == held.generation + 1


async def test_invalid_key_health_is_shared_between_workers(pg_db):
    from autobuild_json.gateway.proxy.pg_leases import PgLeaseStore
    first, other = PgLeaseStore(pg_db), PgLeaseStore(pg_db)
    await first.disable_resource("kiot:fingerprint")
    assert await other.is_disabled("kiot:fingerprint")
    assert not await other.is_disabled("kiot:other-fingerprint")


async def test_claim_does_not_outlive_deadline_while_lease_row_is_locked(pg_db):
    from autobuild_json.gateway.proxy.pg_leases import PgLeaseStore

    store = PgLeaseStore(pg_db)
    resource = "locked-key-fingerprint"
    initial = await store.claim(
        resource,
        uuid4(),
        datetime.now(timezone.utc) + timedelta(seconds=60),
    )
    await store.release(initial)

    async with pg_db.sessions.begin() as blocker:
        await blocker.execute(
            text("SELECT resource FROM proxy_leases WHERE resource=:resource FOR UPDATE"),
            {"resource": resource},
        )
        pending = asyncio.create_task(
            store.claim(
                resource,
                uuid4(),
                datetime.now(timezone.utc) + timedelta(seconds=0.1),
            )
        )
        try:
            with pytest.raises(GatewayError, match="deadline_exceeded") as error:
                await asyncio.wait_for(pending, 0.2)
            assert error.value.status == 504
            assert error.value.stage == "proxy"
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async with pg_db.sessions() as session:
        row = (await session.execute(
            text("SELECT owner FROM proxy_leases WHERE resource=:resource"),
            {"resource": resource},
        )).one()
    assert row.owner is None


async def test_release_lock_timeout_is_bounded_and_redacted(pg_db):
    from autobuild_json.gateway.proxy.pg_leases import PgLeaseStore

    store = PgLeaseStore(pg_db)
    resource = "locked-release-fingerprint"
    token = await store.claim(
        resource,
        uuid4(),
        datetime.now(timezone.utc) + timedelta(seconds=60),
    )
    blocker = pg_db.sessions()
    await blocker.begin()
    try:
        await blocker.execute(
            text("SELECT resource FROM proxy_leases WHERE resource=:resource FOR UPDATE"),
            {"resource": resource},
        )
        started = asyncio.get_running_loop().time()
        with pytest.raises(GatewayError, match="deadline_exceeded") as error:
            await asyncio.wait_for(store.release(token), 1)
        assert error.value.status == 504
        assert error.value.stage == "proxy"
        assert asyncio.get_running_loop().time() - started < 0.8
    finally:
        await blocker.rollback()
        await blocker.close()


async def test_release_joins_bounded_cleanup_when_cancelled(pg_db):
    from autobuild_json.gateway.proxy.pg_leases import PgLeaseStore

    store = PgLeaseStore(pg_db)
    resource = "cancelled-release-fingerprint"
    token = await store.claim(
        resource,
        uuid4(),
        datetime.now(timezone.utc) + timedelta(seconds=60),
    )
    blocker = pg_db.sessions()
    await blocker.begin()
    try:
        await blocker.execute(
            text("SELECT resource FROM proxy_leases WHERE resource=:resource FOR UPDATE"),
            {"resource": resource},
        )
        pending = asyncio.create_task(store.release(token))
        await asyncio.sleep(0.05)
        started = asyncio.get_running_loop().time()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert asyncio.get_running_loop().time() - started < 0.8
        assert pending.done()
    finally:
        await blocker.rollback()
        await blocker.close()
