import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.providers.catalog import Catalog
from autobuild_json.gateway.providers.limits import ProviderLimits
from tests.gateway.harness import gateway_environment
from .test_catalog import catalog_case
from .test_gateway import BODY
from .test_rate_limits import add_backup

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
REQUEST = SimpleNamespace(model="public", required_capabilities={"text"})


@pytest.mark.parametrize("scope", ["provider", "credential"])
async def test_concurrent_workers_cannot_shorten_an_existing_cooldown(pg_db, scope):
    catalog, _, provider, credential, _ = await catalog_case(pg_db)
    target = credential if scope == "credential" else None
    table, identity = ("credentials", credential) if target else ("providers", provider)
    await catalog.cooldown(provider, target, 600)
    async with pg_db.sessions() as session:
        original = await session.scalar(text(f"SELECT cooldown_until FROM {table} WHERE id=:id"), {"id": identity})
    other_worker = Catalog(pg_db, catalog.vault)
    await asyncio.gather(catalog.cooldown(provider, target, 60), other_worker.cooldown(provider, target, 0))
    async with pg_db.sessions() as session:
        actual = await session.scalar(text(f"SELECT cooldown_until FROM {table} WHERE id=:id"), {"id": identity})
    assert actual >= original
    await other_worker.cooldown(provider, target, 1200)
    async with pg_db.sessions() as session:
        assert await session.scalar(text(f"SELECT cooldown_until FROM {table} WHERE id=:id"), {"id": identity}) > actual


async def test_all_cooled_routes_return_earliest_eligible_route_delay(pg_db):
    async with gateway_environment(pg_db) as env:
        backup = await add_backup(env)
        await env.engine.catalog.cooldown(env.provider_id, None, 120)
        await env.engine.catalog.cooldown(env.provider_id, env.credential_id, 300)
        await env.engine.catalog.cooldown(backup, None, 60)
        response = await env.client.post("/v1/chat/completions", json=BODY,
                                         headers={"Authorization": "Bearer " + env.secret})
        assert response.status_code == 429
        assert 58 <= int(response.headers["retry-after"]) <= 60
        assert response.json()["error"]["code"] == "rate_limited"
        assert not env.upstream_requests
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM requests")) == 0


async def test_disabled_backup_cannot_supply_an_earlier_retry_hint(pg_db):
    async with gateway_environment(pg_db) as env:
        backup = await add_backup(env)
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE credentials SET enabled=false WHERE provider_id=:id"), {"id": backup})
        await env.engine.catalog.cooldown(backup, None, 1)
        await env.engine.catalog.cooldown(env.provider_id, None, 120)
        await env.engine.catalog.cooldown(env.provider_id, env.credential_id, 300)
        response = await env.client.post("/v1/chat/completions", json=BODY,
                                         headers={"Authorization": "Bearer " + env.secret})
        assert response.status_code == 429
        assert 298 <= int(response.headers["retry-after"]) <= 300
        assert not env.upstream_requests


async def test_cooldown_does_not_mask_unsupported_capabilities_or_key_permissions(pg_db):
    catalog, principal, provider, _, _ = await catalog_case(pg_db)
    await catalog.cooldown(provider, None, 120)
    with pytest.raises(GatewayError, match="unsupported_feature") as error:
        await catalog.candidates(principal, SimpleNamespace(model="public", required_capabilities={"vision"}))
    assert error.value.retry_after is None
    with pytest.raises(GatewayError, match="permission_denied") as error:
        await catalog.candidates(principal, SimpleNamespace(model="private", required_capabilities={"text"}))
    assert error.value.retry_after is None


@pytest.mark.parametrize("scope", ["provider", "credential"])
async def test_stale_route_snapshot_cannot_admit_during_cooldown(pg_db, scope):
    catalog, principal, provider, credential, _ = await catalog_case(pg_db)
    route = (await catalog.candidates(principal, REQUEST))[0]
    await catalog.cooldown(provider, credential if scope == "credential" else None, 120)
    with pytest.raises(GatewayError, match="rate_limited") as error:
        async with ProviderLimits(pg_db).acquire(route, datetime.now(timezone.utc) + timedelta(seconds=30)):
            pytest.fail("Cooldown must be checked again before dispatch")
    assert error.value.status == 429 and 118 <= error.value.retry_after <= 120
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM provider_admissions")) == 0
