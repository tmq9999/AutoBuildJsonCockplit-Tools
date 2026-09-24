import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.gateway.codex_admin_support import private_env
from tests.gateway.integration.test_ledger import ledger_case, bucket
from autobuild_json.gateway.metering.records import Usage

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_same_request_concurrently_replays_and_cross_key_conflicts(pg_db, settings):
    _, _, issued, _ = await ledger_case(pg_db)
    _, _, other, _ = await ledger_case(pg_db)
    async with private_env(pg_db, settings) as (client, services):
        command = {"request_id": str(uuid4()), "version": 1, "amount_micro": "25", "reason": "Reviewed grant"}
        path = f"/api/service/keys/{issued.key_id}/quota-adjust"
        results = await asyncio.wait_for(asyncio.gather(*(client.post(path, json=command) for _ in range(5))), 5)
        assert [r.status_code for r in results] == [200] * 5
        assert all(r.json() == results[0].json() for r in results)
        command["request_id"] = str(uuid4())
        command["version"] = 2
        other_command = dict(command, version=1)
        results = await asyncio.wait_for(asyncio.gather(client.post(path, json=command),
            client.post(f"/api/service/keys/{other.key_id}/quota-adjust", json=other_command)), 5)
        assert sorted(r.status_code for r in results) == [200, 409]


async def test_grant_is_exact_replayable_and_preserves_accounting(pg_db, settings):
    ledger, admission, issued, _ = await ledger_case(pg_db, total_micro=100_000_000, day_micro=10_000, month_micro=20_000)
    request = admission(2000)
    await ledger.reserve(request)
    await ledger.mark_dispatched(request.request_id, uuid4())
    await ledger.settle(request.request_id, Usage(1000, 85))
    async with private_env(pg_db, settings) as (client, services):
        path = f"/api/service/keys/{issued.key_id}/quota-adjust"
        payload = {"request_id": str(uuid4()), "version": 1, "amount_micro": "1085", "reason": "Restore consumed allocation"}
        response = await client.post(path, json=payload)
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["before_limit_micro"] == "100000000" and receipt["after_limit_micro"] == "100001085"
        assert receipt["version_before"] == 1 and receipt["version_after"] == 2
        assert (await client.post(path, json=payload)).json() == receipt
        assert (await client.post(path, json=dict(payload, amount_micro="2"))).status_code == 409
        assert (await client.post(path, json=dict(payload, request_id=str(uuid4())))).status_code == 409
        assert await bucket(pg_db, issued.key_id) == (1085, 0)
        async with pg_db.sessions() as session:
            policy = await session.scalar(text("SELECT policy FROM api_keys WHERE id=:id"), {"id": issued.key_id})
            assert policy["total_micro"] == 100001085 and policy["day_micro"] == 10000 and policy["month_micro"] == 20000
            assert await session.scalar(text("SELECT count(*) FROM usage_ledger WHERE request_id=:id"), {"id": request.request_id}) == 1
            assert await session.scalar(text("SELECT count(*) FROM key_quota_adjustments")) == 1
            assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='key.quota_granted'")) == 1
        keys = (await client.get("/api/service/keys")).json()
        assert keys[0]["balance"] == {"total_micro": "100001085", "spent_micro": "1085", "held_micro": "0", "available_micro": "100000000"}


async def test_grant_restores_exact_100_million_token_available_quota(pg_db, settings):
    ledger, admission, issued, _ = await ledger_case(pg_db, total_micro=100_000_000_000_000,
                                                   day_micro=10_000_000_000, month_micro=20_000_000_000)
    request = replace(admission(2000), input_micro=1_000_000, output_micro=1_000_000)
    await ledger.reserve(request)
    await ledger.mark_dispatched(request.request_id, uuid4())
    await ledger.settle(request.request_id, Usage(1000, 85))
    async with private_env(pg_db, settings) as (client, services):
        result = await client.post(f"/api/service/keys/{issued.key_id}/quota-adjust", json={
            "request_id": str(uuid4()), "version": 1, "amount_micro": "1085000000", "reason": "Restore consumed allocation"})
        assert result.status_code == 200
        assert result.json()["after_limit_micro"] == "100001085000000"
        assert await bucket(pg_db, issued.key_id) == (1_085_000_000, 0)
        balance = (await client.get("/api/service/keys")).json()[0]["balance"]
        assert balance == {"total_micro": "100001085000000", "spent_micro": "1085000000",
                           "held_micro": "0", "available_micro": "100000000000000"}
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 1


async def test_concurrent_settlement_and_grant_preserve_total_and_held(pg_db, settings):
    ledger, admission, issued, _ = await ledger_case(pg_db, total_micro=100_000_000)
    request = admission(2000)
    await ledger.reserve(request)
    await ledger.mark_dispatched(request.request_id, uuid4())
    async with private_env(pg_db, settings) as (client, services):
        response, _ = await asyncio.gather(client.post(f"/api/service/keys/{issued.key_id}/quota-adjust",
            json={"request_id": str(uuid4()), "version": 1, "amount_micro": "1085", "reason": "Reviewed grant"}),
            ledger.settle(request.request_id, Usage(1000, 85)))
        assert response.status_code == 200
        assert await bucket(pg_db, issued.key_id) == (1085, 0)
        assert (await client.get("/api/service/keys")).json()[0]["balance"]["available_micro"] == "100000000"


async def test_grant_validation_zero_overflow_unlimited_and_cross_key_reuse(pg_db, settings):
    _, _, issued, identity = await ledger_case(pg_db, total_micro=10**38-2)
    _, _, unlimited, _ = await ledger_case(pg_db, total_micro=None)
    async with private_env(pg_db, settings) as (client, services):
        path = f"/api/service/keys/{issued.key_id}/quota-adjust"
        body = {"request_id": str(uuid4()), "version": 1, "amount_micro": "0", "reason": "Reviewed noop"}
        assert (await client.post(path, json=dict(body, amount_micro="2"))).status_code == 422
        for amount in (-1, True, 1.5, "1e2", str(10**38)):
            assert (await client.post(path, json=dict(body, amount_micro=amount))).status_code == 422
        assert (await client.post(f"/api/service/keys/{unlimited.key_id}/quota-adjust", json=body)).status_code == 409
        noop = await client.post(path, json=body)
        assert noop.status_code == 200 and noop.json()["version_after"] == 2
        assert (await client.post(f"/api/service/keys/{unlimited.key_id}/quota-adjust", json=body)).status_code == 409
        await identity.revoke(issued.key_id, 2)
        assert (await client.post(path, json=body)).json() == noop.json()
        assert (await client.post(path, json=dict(body, request_id=str(uuid4()), version=3))).status_code == 401


async def test_unlimited_balance_keeps_known_spent_and_held(pg_db, settings):
    ledger, admission, issued, _ = await ledger_case(pg_db, total_micro=None)
    request = admission(70)
    await ledger.reserve(request)
    await ledger.mark_dispatched(request.request_id, uuid4())
    await ledger.settle(request.request_id, Usage(30, 10))
    await ledger.reserve(admission(25))
    async with private_env(pg_db, settings) as (client, services):
        key = (await client.get("/api/service/keys")).json()[0]
        assert key["balance"] == {"total_micro":None, "spent_micro":"40", "held_micro":"25", "available_micro":None}
