"""The service overview reports stored facts, never provider/process health."""
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from autobuild_json.gateway.identity.policy import KeyPolicy
from autobuild_json.gateway.metering.records import Usage
from autobuild_json.gateway.settings import ServiceSettings
from tests.gateway.codex_admin_support import ORIGIN, account, private_env
from tests.gateway.integration.test_ledger import ledger_case

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
PATH = "/api/service/codex-service/status"


async def test_status_is_private_storage_only_and_does_not_claim_running(pg_db, settings):
    from autobuild_json.gateway.http.app import create_gateway_app
    async with private_env(pg_db, settings, login=False) as (client, services):
        assert (await client.get(PATH)).status_code == 401
        await client.post("/api/session", headers={"Origin": ORIGIN}, json={"token": "test-admin-token"})
        result = await client.get(PATH)
        assert result.status_code == 200, result.text
        assert result.json() == {"status": "configured", "base_url": "http://127.0.0.1:8788/v1",
            "accounts": {"total": 0, "active": 0, "reauth_required": 0, "refresh_uncertain": 0, "unverified": 0, "disabled": 0},
            "keys": {"total": 0, "enabled": 0},
                    "usage": {"requests": 0, "completed": 0, "failed": 0, "pending": 0,
                              "unknown_usage_requests": 0, "unknown_detail_requests": 0,
                              "input_tokens": "0", "cached_read": "0", "cached_write": "0", "output_tokens": "0",
                              "reasoning": "0", "total_tokens": "0", "uncached_input_tokens": "0",
                              "charged_micro": "0", "held_micro": "0"},
            "version": "codex-http-v2",
            "capacity": {"admission": services.engine.admission.snapshot(),
                          "provider": services.engine.provider_limits.snapshot(),
                          "proxy": services.engine.proxies.snapshot()}}
        assert "no-store" in result.headers["cache-control"].split(", ")
        bad = await client.get(PATH + "?token=DO-NOT-ECHO")
        assert bad.status_code == 400 and "DO-NOT-ECHO" not in bad.text
        public = create_gateway_app(services.identity, services.catalog, services.engine, allowed_hosts=("public",))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="http://public") as other:
            assert (await other.get(PATH)).status_code == 404
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM audit_events")) == 0


async def test_status_reads_serving_process_capacity_snapshot(pg_db, settings):
    async with private_env(pg_db, settings) as (client, services):
        await client.post("/api/session", headers={"Origin": ORIGIN}, json={"token": "test-admin-token"})
        snapshot = {"admission": {"capacity": 100, "inflight": 3, "queued": 2},
                    "provider": {"wait_attempts": 4}, "proxy": {"capacity_signals": 1}}
        async with pg_db.sessions.begin() as session:
            await session.execute(text(
                "INSERT INTO gateway_capacity_snapshots(id,snapshot,updated_at) "
                "VALUES (1,CAST(:snapshot AS jsonb),clock_timestamp()) "
                "ON CONFLICT (id) DO UPDATE SET snapshot=EXCLUDED.snapshot,updated_at=EXCLUDED.updated_at"),
                {"snapshot": json.dumps(snapshot)})
        result = await client.get(PATH)
        assert result.status_code == 200
        assert result.json()["capacity"] == snapshot


async def test_status_counts_accounts_and_usable_keys_without_revealing_identities(pg_db, settings):
    async with private_env(pg_db, settings) as (client, services):
        for number, state in enumerate(("active", "reauth_required", "refresh_uncertain", "unverified", "active")):
            identity = await account(services, str(number))
            async with pg_db.sessions.begin() as session:
                await session.execute(text("UPDATE credentials SET health=:state,enabled=:enabled WHERE id=:id"),
                                      {"state": state, "enabled": number != 4, "id": identity})
        # A lookalike OAuth row with another issuer is not a Codex account.
        other = await account(services, "other")
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE credentials SET issuer='https://other.invalid' WHERE id=:id"), {"id": other})
        owner = await services.identity.create_customer("DO-NOT-ECHO-CUSTOMER")
        keys = [await services.identity.create_key(owner, KeyPolicy(model_ids={"m"}, protocols={"openai"})) for _ in range(4)]
        await services.identity.revoke(keys[1].key_id, keys[1].version)
        await services.identity.update_policy(keys[2].key_id, keys[2].version,
            KeyPolicy(model_ids={"m"}, protocols={"openai"}, enabled=False))
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE api_keys SET expires_at=:expiry WHERE id=:id"),
                                  {"expiry": datetime.now(timezone.utc)-timedelta(seconds=1), "id": keys[3].key_id})
        services.service_settings = ServiceSettings(host="::1", port=9898, _env_file=None)
        result = await client.get(PATH)
        assert result.status_code == 200
        assert result.json()["base_url"] == "http://[::1]:9898/v1"
        assert result.json()["accounts"] == {"total": 5, "active": 1, "reauth_required": 1,
            "refresh_uncertain": 1, "unverified": 1, "disabled": 1}
        assert result.json()["keys"] == {"total": 4, "enabled": 1}
        for secret in ("synthetic@example.com", "account-test", "synthetic-access", "synthetic-refresh",
                       "encrypted_secret", "DO-NOT-ECHO-CUSTOMER", *(key.secret for key in keys)):
            assert secret not in result.text


async def test_status_sums_only_settled_serving_usage_with_exact_decimal_strings(pg_db, settings):
    ledger, admission, _, _ = await ledger_case(pg_db, total_micro=10**30)
    async with private_env(pg_db, settings) as (client, services):
        huge = 2**53 + 1
        for _ in range(2):
            request = admission(huge + 10)
            await ledger.reserve(request)
            rejected, serving = uuid4(), uuid4()
            await ledger.mark_dispatched(request.request_id, rejected)
            await ledger.mark_rejected(request.request_id, rejected, "rejected_before_generation")
            await ledger.mark_dispatched(request.request_id, serving)
            await ledger.settle(request.request_id, Usage(huge, 5, cached_read=3, cached_write=2))
            async with pg_db.sessions.begin() as session:
                await session.execute(text("UPDATE attempts SET usage=CAST(:usage AS jsonb) WHERE id=:id"),
                                      {"id": rejected, "usage": json.dumps({"input_tokens": 999, "output_tokens": 999})})
                await session.execute(text("UPDATE attempts SET status='completed',usage=CAST(:usage AS jsonb) WHERE id=:id"),
                    {"id": serving, "usage": json.dumps({"input_tokens": huge, "cached_read": 3, "cached_write": 2, "output_tokens": 5})})
        pending = admission(100)
        await ledger.reserve(pending)
        await ledger.mark_dispatched(pending.request_id, uuid4())
        await ledger.mark_pending(pending.request_id, "usage_missing")
        failed = admission(100)
        await ledger.reserve(failed)
        await ledger.release_unspent(failed.request_id, "not_dispatched")
        result = await client.get(PATH)
        assert result.status_code == 200, result.text
        assert result.json()["usage"] == {"requests": 4, "completed": 2, "failed": 1, "pending": 1,
            "input_tokens": str(huge * 2), "cached_read": "6", "cached_write": "4", "output_tokens": "10",
                "reasoning": None, "total_tokens": str((huge + 5) * 2),
                "uncached_input_tokens": str((huge - 5) * 2),
                "unknown_usage_requests": 0, "unknown_detail_requests": 2,
                "charged_micro": str((huge + 5) * 2), "held_micro": "100"}


async def test_status_unknown_usage_and_ambiguous_legacy_serving_attempt_are_not_zero(pg_db, settings):
    ledger, admission, _, _ = await ledger_case(pg_db)
    async with private_env(pg_db, settings) as (client, services):
        request, attempt = admission(100), uuid4()
        await ledger.reserve(request)
        await ledger.mark_dispatched(request.request_id, attempt)
        await ledger.settle(request.request_id, Usage(10, 2))
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE attempts SET status='completed',usage=CAST(:usage AS jsonb) WHERE id=:id"),
                                  {"id": attempt, "usage": json.dumps({"input_tokens": 10, "output_tokens": 2})})
        usage = (await client.get(PATH)).json()["usage"]
        assert usage["input_tokens"] == "10" and usage["charged_micro"] == "12"
        assert usage["cached_read"] is None and usage["cached_write"] is None
        async with pg_db.sessions.begin() as session:
            await session.execute(text("INSERT INTO attempts(id,request_id,status,usage) VALUES (:id,:request,'completed',CAST(:usage AS jsonb))"),
                {"id": uuid4(), "request": request.request_id, "usage": json.dumps({"input_tokens": 5, "output_tokens": 2})})
        usage = (await client.get(PATH)).json()["usage"]
        assert usage["requests"] == 1 and usage["completed"] == 1
        assert usage['unknown_usage_requests'] == 1
        assert all(usage[name] == '0' for name in ("input_tokens", "cached_read", "cached_write", "output_tokens", "charged_micro"))
