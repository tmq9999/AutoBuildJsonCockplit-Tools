import json
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.gateway.codex_admin_support import account, private_env
from tests.gateway.integration.test_ledger import ledger_case
from autobuild_json.gateway.metering.records import Usage

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_retry_reports_charge_only_serving_account_and_keep_legacy_unknown(pg_db, settings):
    ledger, admission, _, _ = await ledger_case(pg_db, total_micro=10**30)
    async with private_env(pg_db, settings) as (client, services):
        first, second = await account(services, "a"), await account(services, "b")
        request = admission(2000)
        await ledger.reserve(request)
        rejected, completed = uuid4(), uuid4()
        await ledger.mark_dispatched(request.request_id, rejected)
        await ledger.mark_rejected(request.request_id, rejected, "rejected_before_generation")
        await ledger.mark_dispatched(request.request_id, completed)
        await ledger.settle(request.request_id, Usage(1000, 85, cached_read=50))
        legacy = admission(100)
        await ledger.reserve(legacy)
        legacy_attempt = uuid4()
        await ledger.mark_dispatched(legacy.request_id, legacy_attempt)
        await ledger.settle(legacy.request_id, Usage(10, 5))
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE attempts SET credential_id=:credential WHERE id=:id"), {"credential": first, "id": rejected})
            await session.execute(text("UPDATE attempts SET credential_id=:credential,status='completed',usage=CAST(:usage AS jsonb) WHERE id=:id"),
                                  {"credential": second, "id": completed, "usage": json.dumps({"input_tokens": 1000, "output_tokens": 85, "cached_read": 50, "cached_write": 0, "reasoning": 0})})
            await session.execute(text("UPDATE attempts SET status='completed',usage=CAST(:usage AS jsonb) WHERE id=:id"),
                                  {"id": legacy_attempt, "usage": json.dumps({"input_tokens": 10, "output_tokens": 5})})
        response = await client.get("/api/service/usage/accounts")
        assert response.status_code == 200, response.text
        rows = {r["credential_id"]: r for r in response.json()["items"]}
        assert rows[str(first)]["charged_micro"] == "0"
        assert rows[str(second)]["charged_micro"] == "1085"
        assert rows[str(second)]["input_tokens"] == "1000" and rows[str(second)]["cached_read"] == "50"
        assert rows[None]["attribution"] == "unknown" and rows[None]["charged_micro"] == "15"
        attempts = (await client.get("/api/service/usage/requests")).json()["items"]
        assert len(attempts) == 3
        assert sum(int(r["charged_micro"]) for r in attempts) == 1100
        filtered = await client.get("/api/service/usage/requests", params={"credential_id": str(second), "model_id": "m", "limit": 1})
        assert filtered.status_code == 200 and len(filtered.json()["items"]) == 1
        assert filtered.json()["items"][0]["id"] == str(completed)
        assert filtered.json()["summary"]["charged_micro"] == "1085"
        for endpoint in ("requests", "accounts"):
            rejected_report = (await client.get("/api/service/usage/" + endpoint,
                params={"credential_id": str(first)})).json()
            summary = rejected_report["summary"]
            assert summary["charged_micro"] == "0"
            assert summary["total_tokens"] == "0"
            assert summary["completed"] == 0
            assert summary["failed"] == 1
            assert summary["unknown_usage_requests"] == 0


async def test_account_summary_holds_belong_only_to_current_attempt(pg_db, settings):
    ledger, admission, _, _ = await ledger_case(pg_db, total_micro=10000)
    async with private_env(pg_db, settings) as (client, services):
        first, second = await account(services, "first"), await account(services, "second")
        request, rejected, pending = admission(200), uuid4(), uuid4()
        await ledger.reserve(request)
        await ledger.mark_dispatched(request.request_id, rejected)
        await ledger.mark_rejected(request.request_id, rejected, "rejected_before_generation")
        await ledger.mark_dispatched(request.request_id, pending)
        await ledger.mark_pending(request.request_id, "usage_missing")
        async with pg_db.sessions.begin() as session:
            for attempt, credential in ((rejected, first), (pending, second)):
                await session.execute(text("UPDATE attempts SET credential_id=:credential WHERE id=:id"),
                                      {"credential": credential, "id": attempt})
        for credential, held, failed, waiting in ((first, "0", 1, 0), (second, "200", 0, 1)):
            result = (await client.get("/api/service/usage/requests",
                params={"credential_id": str(credential)})).json()
            assert result["summary"]["held_micro"] == held
            assert result["summary"]["failed"] == failed
            assert result["summary"]["pending"] == waiting


async def test_report_filters_are_bounded_and_aggregate_counts_are_exact(pg_db, settings):
    ledger, admission, _, _ = await ledger_case(pg_db, total_micro=10**30)
    async with private_env(pg_db, settings) as (client, services):
        identity = await account(services)
        for _ in range(2):
            request = admission(2**53+1)
            await ledger.reserve(request)
            attempt = uuid4()
            await ledger.mark_dispatched(request.request_id, attempt)
            await ledger.settle(request.request_id, Usage(2**53+1, 0))
            async with pg_db.sessions.begin() as session:
                await session.execute(text("UPDATE attempts SET credential_id=:credential,status='completed',usage=CAST(:usage AS jsonb) WHERE id=:id"),
                    {"credential": identity, "id": attempt, "usage": json.dumps({"input_tokens": 2**53+1, "output_tokens": 0})})
        row = (await client.get("/api/service/usage/accounts")).json()["items"][0]
        assert row["input_tokens"] == "18014398509481986" and row["attempt_count"] == "2"
        page = (await client.get("/api/service/usage/requests?limit=1")).json()
        assert page["next_after"] == page["items"][0]["id"]
        following = (await client.get("/api/service/usage/requests", params={"limit": 1, "after": page["next_after"]})).json()
        assert following["items"][0]["id"] != page["items"][0]["id"] and following["next_after"] is None
        for query in ("from=2026-01-01", "from=2026-01-01T00:00:00Z&to=2026-03-01T00:00:00Z",
                      "from=2026-03-01T00:00:00Z&to=2026-01-01T00:00:00Z", "limit=0", "after=bad", "credential_id=bad",
                      "model_id=m&model_id=x", "token=SECRET", "model_id=%00", "limit=201"):
            for endpoint in ("accounts", "requests"):
                result = await client.get("/api/service/usage/" + endpoint + "?" + query)
                assert result.status_code in {400, 422}, (query, result.text)


async def test_real_retry_charge_and_public_usage_do_not_change(pg_db, settings):
    import httpx
    from .test_codex_pool_dispatch import codex_environment, BODY
    from .test_responses_gateway import native_wire
    def reply(request):
        return httpx.Response(429) if request.headers["chatgpt-account-id"] == "account-0" else httpx.Response(200, content=native_wire())
    async with codex_environment(pg_db, respond=reply) as env:
        result = await env.client.post("/v1/responses", json=BODY, headers=env.headers)
        assert result.status_code == 200
        assert result.json()["usage"]["input_tokens"] == 4
        assert type(result.json()["usage"]["input_tokens"]) is int
        async with private_env(pg_db, settings) as (client, _):
            rows = (await client.get("/api/service/usage/requests")).json()["items"]
            assert len(rows) == 2
            rows = {r["status"]: r for r in rows}
            assert rows["rejected"]["charged_micro"] == "0" and rows["rejected"]["usage"] is None
            assert rows["completed"]["charged_micro"] == "23" and rows["completed"]["computed_micro"] == "23"
            assert rows["completed"]["usage"]["input_tokens"] == "4"
            assert rows["completed"]["credential_id"] == str(env.bindings[1].credential_id)


async def test_group_pages_sum_whole_window_and_pending_usage_is_unknown(pg_db, settings):
    ledger, admission, _, _ = await ledger_case(pg_db, total_micro=10000)
    async with private_env(pg_db, settings) as (client, services):
        first, second = await account(services, "first"), await account(services, "second")
        for credential in (None, first, first, second):
            request, attempt = admission(100), uuid4()
            await ledger.reserve(request)
            await ledger.mark_dispatched(request.request_id, attempt)
            await ledger.settle(request.request_id, Usage(120, 0))
            async with pg_db.sessions.begin() as session:
                await session.execute(text("UPDATE attempts SET credential_id=:credential,status='completed',usage=CAST(:usage AS jsonb),cost=CAST(:cost AS jsonb) WHERE id=:id"),
                    {"credential": credential, "id": attempt, "usage": json.dumps({"input_tokens":120,"output_tokens":0}),
                     "cost": json.dumps({"amount":"0.123456789012", "currency":"USD", "source":"estimated"})})
        pending, attempt = admission(200), uuid4()
        await ledger.reserve(pending)
        await ledger.mark_dispatched(pending.request_id, attempt)
        await ledger.mark_pending(pending.request_id, "usage_missing")
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE attempts SET credential_id=:credential WHERE id=:id"), {"credential": second, "id": attempt})
        pages, cursor = [], None
        for _ in range(3):
            params = {"limit":1}
            if cursor is not None:
                params["after"] = cursor
            page = (await client.get("/api/service/usage/accounts", params=params)).json()
            pages.extend(page["items"])
            cursor = page["next_after"]
        assert len(pages) == 3 and cursor is None
        assert pages[0]["attribution"] == "unknown" and pages[0]["credential_id"] is None
        rows = {row["credential_id"]: row for row in pages}
        assert rows[str(first)]["input_tokens"] == "240" and rows[str(first)]["attempt_count"] == "2"
        assert rows[str(first)]["charged_micro"] == "200" and rows[str(first)]["computed_micro"] == "240"
        assert rows[str(first)]["upstream_costs"] == [{"currency":"USD", "amount":"0.246913578024"}]
        assert rows[str(second)]["unknown_usage_count"] == "1" and rows[str(second)]["held_micro"] == "200"
        pending_row = next(row for row in (await client.get("/api/service/usage/requests")).json()["items"] if row["id"] == str(attempt))
        assert pending_row["usage"] is None and pending_row["computed_micro"] is None and pending_row["charged_micro"] == "0"
        assert pending_row["held_micro"] == "200"
        now = datetime.now(timezone.utc)
        params = {"from": (now-timedelta(hours=1)).isoformat(), "to": now.isoformat(), "credential_id":str(first), "model_id":"m", "limit":1, "after":str(uuid4())}
        assert (await client.get("/api/service/usage/accounts", params=params)).status_code == 200
        assert (await client.get("/api/service/usage/requests", params={"model_id":"m' OR 1=1 --"})).json()["items"] == []
        for params in ({"from": (now-timedelta(days=32)).isoformat()}, {"from": (now+timedelta(days=1)).isoformat()}, {"to":"2026-01-01T00:00:00"}):
            assert (await client.get("/api/service/usage/accounts", params=params)).status_code == 422


async def test_attempt_computed_quota_and_money_are_separate_from_settlement(pg_db, settings):
    ledger, admission, _, _ = await ledger_case(pg_db, total_micro=10000)
    async with private_env(pg_db, settings) as (client, services):
        identity = await account(services)
        request = replace(admission(100), input_micro=2, output_micro=3, cache_read_micro=1, cache_write_micro=4)
        await ledger.reserve(request)
        attempt = uuid4()
        await ledger.mark_dispatched(request.request_id, attempt)
        await ledger.mark_pending(request.request_id, "usage_missing")
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE attempts SET credential_id=:credential,usage=CAST(:usage AS jsonb),cost=CAST(:cost AS jsonb) WHERE id=:id"),
                {"credential":identity,"id":attempt,"usage":json.dumps({"input_tokens":100,"output_tokens":5,"cached_read":20,"cached_write":10,"reasoning":2}),
                 "cost":json.dumps({"currency":"EUR","amount":"0.001000000001","source":"estimated"})})
        row = (await client.get("/api/service/usage/requests")).json()["items"][0]
        assert row["computed_micro"] == "215" and row["charged_micro"] == "0" and row["held_micro"] == "400"
        account_row = (await client.get("/api/service/usage/accounts")).json()["items"][0]
        assert account_row["computed_micro"] == "215" and account_row["charged_micro"] == "0"
        assert account_row["upstream_costs"] == [{"currency":"EUR","amount":"0.001000000001"}]


@pytest.mark.parametrize("endpoint", ["requests", "accounts"])
@pytest.mark.parametrize("includes_admission", [False, True])
async def test_report_rows_and_summary_share_admission_window(pg_db, settings, endpoint, includes_admission):
    from autobuild_json.gateway.metering.records import Bounds

    ledger, admission, _, _ = await ledger_case(pg_db)
    async with private_env(pg_db, settings) as (client, services):
        credential = await account(services)
        request = replace(admission(100), bounds=Bounds(100, 20))
        attempt = uuid4()
        await ledger.reserve(request)
        await ledger.mark_dispatched(request.request_id, attempt)
        await ledger.settle(request.request_id, Usage(10, 5), attempt_id=attempt)
        now = datetime.now(timezone.utc)
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE requests SET admitted_at=:at WHERE id=:id"),
                                  {"id": request.request_id, "at": now-timedelta(minutes=2)})
            # Dispatch can cross the reporting boundary while waiting for a
            # proxy, OAuth refresh or a provider slot.
            await session.execute(text("UPDATE attempts SET started_at=:at,credential_id=:credential WHERE id=:id"),
                {"id": attempt, "at": now-timedelta(minutes=1), "credential": credential})
        boundary = now-timedelta(seconds=90)
        params = {"credential_id": str(credential),
                  "from": (now-timedelta(minutes=3) if includes_admission else boundary).isoformat(),
                  "to": (boundary if includes_admission else now).isoformat()}
        response = await client.get("/api/service/usage/"+endpoint, params=params)
        assert response.status_code == 200
        report = response.json()
        assert report["summary"]["requests"] == int(includes_admission)
        assert report["summary"]["charged_micro"] == ("15" if includes_admission else "0")
        assert len(report["items"]) == int(includes_admission)
        assert sum(int(row["charged_micro"]) for row in report["items"]) == (15 if includes_admission else 0)
        if includes_admission and endpoint == "requests":
            assert datetime.fromisoformat(report["items"][0]["admitted_at"]) == now-timedelta(minutes=2)
            assert datetime.fromisoformat(report["items"][0]["started_at"]) == now-timedelta(minutes=1)
