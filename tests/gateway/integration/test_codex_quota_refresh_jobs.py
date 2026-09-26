import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from autobuild_json.gateway.accounts.quota_refresh_jobs import QuotaRefreshJobs
from autobuild_json.gateway.admin.codex_accounts import AccountViews
from autobuild_json.gateway.errors import GatewayError
from tests.gateway.codex_admin_support import account, private_env

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
PATH = "/api/service/codex-service/quota-refresh"


async def test_persistent_interval_cas_validation_and_default_off(pg_db, settings):
    async with private_env(pg_db, settings) as (client, services):
        await services.quota_refresh_jobs.close()
        initial = (await client.get(PATH)).json()
        assert initial["settings"] == {"version": 0, "interval_minutes": 0, "next_run_at": None}
        await services.quota_refresh_jobs.tick()
        assert (await client.get(PATH)).json()["job"] is None
        for minutes in (-1, 1000, 1.5, True, "5"):
            assert (await client.put(PATH+"/settings", json={"version": 0, "interval_minutes": minutes})).status_code == 422
        for fields in ({}, {"version": 0, "interval_minutes": 2, "force": True}):
            assert (await client.put(PATH+"/settings", json=fields)).status_code == 422
        assert (await client.get(PATH+"?key=SECRET-SENTINEL")).status_code == 400
        saved = await client.put(PATH+"/settings", json={"version": 0, "interval_minutes": 7})
        assert saved.status_code == 200, saved.text
        policy = saved.json()["settings"]
        assert policy["version"] == 1 and policy["interval_minutes"] == 7 and policy["next_run_at"]
        assert (await client.put(PATH+"/settings", json={"version": 0, "interval_minutes": 10})).status_code == 409
        assert (await QuotaRefreshJobs(services).read())["settings"]["interval_minutes"] == 7
        await services.quota_refresh_jobs.tick()  # not due
        assert (await client.get(PATH)).json()["job"] is None
        off = await client.put(PATH+"/settings", json={"version": 1, "interval_minutes": 0})
        assert off.json()["settings"] == {"version": 2, "interval_minutes": 0, "next_run_at": None}


async def test_manual_all_refresh_partial_success_disabled_and_idempotent(pg_db, settings, monkeypatch):
    # The package/CI contract still includes Python 3.10, where TaskGroup is
    # absent.  Bulk refresh must retain its bounded worker behavior there.
    monkeypatch.delattr(asyncio, "TaskGroup", raising=False)
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/usage"):
            return httpx.Response(200, json={"plan_type": "plus"})
        return httpx.Response(500, text="SECRET-SENTINEL")

    async with private_env(pg_db, settings, respond) as (client, services):
        await services.quota_refresh_jobs.close()
        for i in range(3):
            last = await account(services, str(i))
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE credentials SET enabled=false WHERE id=:id"), {"id": last})
        rid = str(uuid4())
        response = await client.post(PATH, json={"request_id": rid})
        assert response.status_code == 202 and response.json()["job"]["state"] == "queued"
        assert calls == []
        assert (await client.post(PATH, json={"request_id": str(uuid4())})).json()["job"]["id"] == rid
        await services.quota_refresh_jobs.tick()
        result = (await client.get(PATH)).json()["job"]
        assert (result["state"], result["total"], result["succeeded"], result["failed"], result["skipped"]) == ("completed", 3, 0, 2, 1)
        assert len(result["errors"]) == 2 and result["errors"][0]["code"] == "codex_credits_unavailable"
        assert result["errors"][0]["email"] == "synthetic@example.com"
        assert "SECRET-SENTINEL" not in str(result)
        assert len(calls) == 4 and all(method == "GET" for method, _ in calls)
        await client.post(PATH, json={"request_id": rid})
        await services.quota_refresh_jobs.tick()
        assert len(calls) == 4  # replay never starts another refresh


async def test_all_1000_accounts_two_workers_coalesce_and_ignore_ui_pages(pg_db, settings, monkeypatch):
    async with private_env(pg_db, settings) as (client, services):
        await services.quota_refresh_jobs.close()
        seed = await account(services)
        async with pg_db.sessions.begin() as session:
            await session.execute(text("""INSERT INTO credentials
                (id,provider_id,encrypted_secret,issuer,account_id,email,enabled,health)
                SELECT gen_random_uuid(),provider_id,encrypted_secret,issuer,'synthetic-'||n,
                    'stress-'||n||'@example.invalid',true,'active'
                FROM credentials CROSS JOIN generate_series(1,999) n WHERE id=:id"""), {"id": seed})
        entered, release = asyncio.Event(), asyncio.Event()
        calls, in_flight, peak = [], 0, 0

        async def refresh(self, identity):
            nonlocal in_flight, peak
            calls.append(identity)
            in_flight += 1
            peak = max(peak, in_flight)
            if len(calls) >= 2:
                entered.set()
            await release.wait()
            in_flight -= 1
            return {"usage_status": "updated", "credits_status": "updated"}

        monkeypatch.setattr(AccountViews, "refresh", refresh)
        await client.post(PATH, json={"request_id": str(uuid4())})
        first = asyncio.create_task(services.quota_refresh_jobs.tick())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await QuotaRefreshJobs(services).tick()  # another process cannot run the same batch
            assert len(calls) == 2
            await client.post(PATH, json={"request_id": str(uuid4())})  # another tab coalesces
        finally:
            release.set()
            await first
        job = (await client.get(PATH)).json()["job"]
        assert job["state"] == "completed" and job["total"] == job["succeeded"] == 1000
        assert len(set(calls)) == 1000 and peak == 2


async def test_schedule_runs_after_due_and_survives_ui_close_then_stop(pg_db, settings, monkeypatch):
    async with private_env(pg_db, settings) as (client, services):
        await services.quota_refresh_jobs.close()
        for i in range(5):
            await account(services, str(i))
        await client.put(PATH+"/settings", json={"version": 0, "interval_minutes": 2})
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE codex_quota_refresh_settings SET next_run_at=now()-interval '1 second'"))
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def refresh(self, identity):
            calls.append(identity)
            if len(calls) >= 2:
                entered.set()
            await release.wait()
            return {"usage_status": "updated", "credits_status": "updated"}

        monkeypatch.setattr(AccountViews, "refresh", refresh)
        other = QuotaRefreshJobs(services)
        task = asyncio.create_task(other.tick())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            state = (await client.get(PATH)).json()
            assert state["job"]["trigger"] == "automatic" and state["settings"]["next_run_at"] is None
            stopped = await client.post(PATH+"/stop", json={"request_id": state["job"]["id"]})
            assert stopped.json()["job"]["stop_requested"] is True
        finally:
            release.set()
            await task
        result = (await client.get(PATH)).json()
        assert result["job"]["state"] == "stopped" and len(calls) == 2
        assert result["settings"]["interval_minutes"] == 2 and result["settings"]["next_run_at"]


async def test_rate_limit_stops_no_reset_and_marks_partial_snapshot(pg_db, settings):
    calls = []

    def respond(request):
        calls.append(request.method)
        return httpx.Response(429, headers={"Retry-After": "60"})

    async with private_env(pg_db, settings, respond) as (client, services):
        await services.quota_refresh_jobs.close()
        for i in range(5):
            await account(services, str(i))
        await client.post(PATH, json={"request_id": str(uuid4())})
        await services.quota_refresh_jobs.tick()
        job = (await client.get(PATH)).json()["job"]
        assert job["state"] == "stopped" and job["result_code"] == "rate_limited"
        assert 1 <= job["failed"] <= 2 and job["succeeded"] == 0
        assert len(calls) <= 2 and all(method == "GET" for method in calls)


async def test_interrupted_worker_is_not_replayed_and_errors_are_safe(pg_db, settings, monkeypatch):
    async with private_env(pg_db, settings) as (client, services):
        await services.quota_refresh_jobs.close()
        await account(services)
        await client.post(PATH, json={"request_id": str(uuid4())})
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE codex_quota_refresh_runs SET state='running'"))
        await services.quota_refresh_jobs.tick()
        job = (await client.get(PATH)).json()["job"]
        assert job["state"] == "interrupted" and job["result_code"] == "worker_interrupted"

        async def failed(self, identity):
            raise GatewayError("reauth_required", 401)

        monkeypatch.setattr(AccountViews, "refresh", failed)
        await client.post(PATH, json={"request_id": str(uuid4())})
        await services.quota_refresh_jobs.tick()
        job = (await client.get(PATH)).json()["job"]
        assert job["state"] == "completed" and job["failed"] == 1
        assert job["errors"][0]["code"] == "reauth_required"
