import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

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
