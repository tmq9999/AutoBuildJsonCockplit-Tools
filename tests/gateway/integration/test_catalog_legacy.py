"""Compatibility wrapper runs the same fenced discovery without a worker."""

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from autobuild_json.gateway.transport.egress import EgressPolicy
from autobuild_json.gateway.transport.http import Transport
from tests.gateway.integration.test_admin import admin_env
from tests.gateway.integration.test_catalog_admin import AUTH, create_source

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def synthetic_transport(respond):
    async def resolver(host, port):
        return ["93.184.216.34"]
    return Transport(EgressPolicy(resolver=resolver), adapter=httpx.MockTransport(respond))


async def test_legacy_discovery_is_durable_and_preserves_source_settings(pg_db):
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        services.transport = synthetic_transport(lambda request: httpx.Response(200, json={"data": [{"id": "a"}], "has_more": False}))
        source, body = await create_source(client)
        await client.put(f"/api/service/catalog/sources/{source['id']}", json=dict(body,
            version=1, mode="openai_cursor", schedule_enabled=True, interval_seconds=300))
        path = f"/api/service/providers/{body['provider_id']}/discover"
        first = await client.post(path, json={})
        assert first.status_code == 200, first.text
        assert first.json()["models"] == ["a"] and first.json()["published"] is False
        assert first.json()["credential_id"] == body["credential_id"]
        second = await client.post(path, json={})
        assert second.status_code == 200, second.text
        assert first.json()["run_id"] != second.json()["run_id"]
        row = await services.sources.get(UUID(source["id"]))
        assert row.mode.value == "openai_cursor" and row.schedule_enabled and row.version == 2
        assert (await services.operations.get(UUID(second.json()["run_id"]))).state == "succeeded"
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM public_models")) == 0


async def test_legacy_busy_source_is_conflict_not_coalesced(pg_db):
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        source, body = await create_source(client)
        expected = (await client.get("/api/service/catalog/sources")).json()["sources"][0]["expected"]
        await client.post(f"/api/service/catalog/sources/{source['id']}/discover",
                         json={"operation_id": str(uuid4()), "expected": expected})
        result = await client.post(f"/api/service/providers/{body['provider_id']}/discover", json={})
        assert result.status_code == 409


async def test_legacy_cancellation_joins_transport_and_durable_cleanup(pg_db):
    entered, closed = asyncio.Event(), asyncio.Event()
    async def respond(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        services.transport = synthetic_transport(respond)
        _, body = await create_source(client)
        before = asyncio.all_tasks()
        task = asyncio.create_task(client.post(f"/api/service/providers/{body['provider_id']}/discover", json={}))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT state FROM provider_operations")) == "cancelled"
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0
        assert not (asyncio.all_tasks() - before)


@pytest.mark.parametrize("status", [401, 403])
async def test_legacy_upstream_auth_rejection_is_502(pg_db, status):
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        services.transport = synthetic_transport(lambda request: httpx.Response(status, text="DO-NOT-ECHO"))
        _, body = await create_source(client)
        result = await client.post(f"/api/service/providers/{body['provider_id']}/discover", json={})
        assert result.status_code == 502 and "DO-NOT-ECHO" not in result.text


async def test_legacy_full_snapshot_not_first_admin_page_and_uuid_credential(pg_db):
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        source, body = await create_source(client)
        second = await services.catalog.put_credential(UUID(body["provider_id"]), "second-synthetic")
        ids = sorted([UUID(body["credential_id"]), second])
        models = [f"model-{index:04}" for index in range(250)]
        services.transport = synthetic_transport(lambda request: httpx.Response(200, json={"data": [{"id": model} for model in models]}))
        result = await client.post(f"/api/service/providers/{body['provider_id']}/discover", json={})
        assert result.status_code == 200, result.text
        assert result.json()["models"] == models and result.json()["credential_id"] == str(ids[0])
        assert (await client.post(f"/api/service/providers/{body['provider_id']}/discover", json={"probe": True})).status_code == 422


async def test_legacy_provider_deadline_closes_request_and_never_partial_success(pg_db):
    from autobuild_json.gateway.routing.records import ProviderConfig
    closed = asyncio.Event()
    async def respond(request):
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        _, body = await create_source(client)
        services.transport = synthetic_transport(respond)
        provider = ProviderConfig(name="short", adapter="openai_compatible", root="https://provider.invalid/v1", timeout=1)
        await services.catalog.update_provider(UUID(body["provider_id"]), 1, provider)
        result = await asyncio.wait_for(client.post(f"/api/service/providers/{body['provider_id']}/discover", json={}), 7)
        assert result.status_code == 504 and "models" not in result.json()
        assert closed.is_set()
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM catalog_entries")) == 0
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0


async def test_enqueue_claimed_is_invisible_to_scheduler_until_running(pg_db, monkeypatch):
    from autobuild_json.gateway.catalog.records import OperationRequest
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        source, _ = await create_source(client)
        identity = UUID(source["id"])
        context = await services.sources.context(identity)
        command = OperationRequest(id=uuid4(), source_id=identity, kind="discover", expected=context.stamp)
        entered, release = asyncio.Event(), asyncio.Event()
        original = services.operations._claim_in
        async def gate(*args):
            entered.set()
            await release.wait()
            return await original(*args)
        monkeypatch.setattr(services.operations, "_claim_in", gate)
        task = asyncio.create_task(services.operations.enqueue_claimed(command, uuid4(), "admin"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            async with pg_db.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM provider_operations WHERE state='queued'")) == 0
            release.set()
            claim = await task
            assert claim.operation_id == command.id
            assert (await services.operations.get(command.id)).state == "running"
            assert await services.operations.claim(command.id, uuid4()) is None
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


async def test_legacy_rate_limit_retains_safe_retry_after(pg_db):
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        _, body = await create_source(client)
        services.transport = synthetic_transport(lambda request: httpx.Response(429, headers={"retry-after": "7"}, text="DO-NOT-ECHO"))
        result = await client.post(f"/api/service/providers/{body['provider_id']}/discover", json={})
        assert result.status_code == 429 and result.headers["retry-after"] == "7"
        assert "DO-NOT-ECHO" not in result.text
