import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.providers.limits import ProviderLimits
from autobuild_json.gateway.routing.records import ProviderConfig
from .test_catalog import catalog_case


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
REQUEST = SimpleNamespace(model="public", required_capabilities={"text"})


async def limited_route(pg_db, *, rpm=60):
    catalog, principal, provider, credential, _ = await catalog_case(pg_db)
    await catalog.update_provider(
        provider,
        1,
        ProviderConfig(
            name="Limited",
            adapter="openai_compatible",
            root="https://provider.invalid/v1",
            concurrency_limit=1,
            rpm_limit=rpm,
        ),
    )
    route = (await catalog.candidates(principal, REQUEST))[0]
    return route, provider, credential


async def active_admissions(pg_db):
    async with pg_db.sessions() as session:
        return await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active"))


async def test_waiting_admission_wakes_when_active_capacity_is_released(pg_db):
    route, _, _ = await limited_route(pg_db)
    limits = ProviderLimits(pg_db)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=2)
    acquired = asyncio.Event()

    async def acquire_second():
        async with limits.acquire(route, deadline, wait=True):
            acquired.set()

    async with limits.acquire(route, deadline):
        second = asyncio.create_task(acquire_second())
        await asyncio.sleep(0.1)
        assert not acquired.is_set()
        assert pg_db.engine.pool.checkedout() == 0

    await asyncio.wait_for(second, 0.5)
    assert acquired.is_set()
    assert await active_admissions(pg_db) == 0


async def test_waiting_admission_expires_without_inserting_a_row(pg_db):
    route, _, _ = await limited_route(pg_db)
    limits = ProviderLimits(pg_db)
    async with limits.acquire(route, datetime.now(timezone.utc) + timedelta(seconds=2)):
        with pytest.raises(GatewayError, match="deadline_exceeded") as error:
            async with limits.acquire(
                route,
                datetime.now(timezone.utc) + timedelta(seconds=0.12),
                wait=True,
            ):
                pytest.fail("An expired waiter must not be admitted")

        assert error.value.status == 504
        assert await active_admissions(pg_db) == 1
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions")) == 1
    assert await active_admissions(pg_db) == 0


async def test_default_non_waiting_admission_still_fails_immediately(pg_db):
    route, _, _ = await limited_route(pg_db)
    limits = ProviderLimits(pg_db)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=2)
    async with limits.acquire(route, deadline):
        started = asyncio.get_running_loop().time()
        with pytest.raises(GatewayError, match="upstream_unavailable") as error:
            async with limits.acquire(route, deadline):
                pytest.fail("The compatibility mode must not wait")

        assert error.value.status == 503
        assert asyncio.get_running_loop().time() - started < 0.1


@pytest.mark.parametrize("rejection", ["disabled", "cooldown", "rpm"])
async def test_wait_mode_does_not_retry_non_capacity_rejections(pg_db, rejection):
    route, provider, credential = await limited_route(pg_db, rpm=1)
    limits = ProviderLimits(pg_db)
    if rejection == "disabled":
        statement = "UPDATE credentials SET enabled=false WHERE id=:id"
        identity = credential
    elif rejection == "cooldown":
        statement = "UPDATE providers SET cooldown_until=clock_timestamp()+interval '1 minute' WHERE id=:id"
        identity = provider
    else:
        async with limits.acquire(route, datetime.now(timezone.utc) + timedelta(seconds=2)):
            pass
        statement = None
        identity = None
    if statement is not None:
        async with pg_db.sessions.begin() as session:
            await session.execute(text(statement), {"id": identity})

    started = asyncio.get_running_loop().time()
    with pytest.raises(GatewayError) as error:
        async with limits.acquire(
            route,
            datetime.now(timezone.utc) + timedelta(seconds=1),
            wait=True,
        ):
            pytest.fail("A permanent, cooldown, or RPM rejection must not wait")

    expected = "rate_limited" if rejection == "cooldown" else "upstream_unavailable"
    assert error.value.code == expected
    assert asyncio.get_running_loop().time() - started < 0.5
