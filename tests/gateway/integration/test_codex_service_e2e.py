import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from tests.gateway.codex_admin_support import account, private_env
from tests.gateway.integration.test_codex_pool_dispatch import BODY, codex_environment
from tests.gateway.integration.test_responses_gateway import native_wire


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def cached_native_wire():
    """Synthetic Codex SSE with inclusive input and cached-read usage."""
    frames = []
    for frame in native_wire().split(b"\n\n"):
        if not frame:
            continue
        lines = frame.splitlines()
        if len(lines) == 2 and lines[0].startswith(b"event: "):
            payload = json.loads(lines[1][len(b"data: "):])
            if payload["type"] == "response.completed":
                payload["response"]["usage"] = {
                    "input_tokens": 1000,
                    "output_tokens": 85,
                    "total_tokens": 1085,
                    "input_tokens_details": {"cached_tokens": 600},
                    "output_tokens_details": {"reasoning_tokens": 0},
                }
                lines[1] = b"data: " + json.dumps(payload, separators=(",", ":")).encode()
        frames.append(b"\n".join(lines))
    return b"\n\n".join(frames) + b"\n\n"


async def ledger_totals(db):
    async with db.sessions() as session:
        return (
            await session.execute(text("SELECT count(*),coalesce(sum(amount_micro),0) FROM usage_ledger"))
        ).one()


async def test_real_codex_service_handoff_composes_customer_and_admin_flows(pg_db, settings):
    public_calls = []

    def public_reply(request):
        public_calls.append(request)
        if request.headers["chatgpt-account-id"] == "account-0":
            return httpx.Response(429, headers={"retry-after": "60"})
        return httpx.Response(200, content=cached_native_wire(), headers={"content-type": "text/event-stream"})

    async with codex_environment(pg_db, respond=public_reply) as env:
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE api_keys SET policy=policy || CAST(:policy AS jsonb) WHERE id=:id"),
                                  {"policy": json.dumps({"total_micro": 100_000_000_000_000}), "id": env.key_id})
        response = await env.client.post("/v1/responses", json=BODY, headers=env.headers)
        assert response.status_code == 200, response.text
        assert response.json()["usage"] == {
            "input_tokens": 1000,
            "output_tokens": 85,
            "total_tokens": 1085,
            "input_tokens_details": {"cached_tokens": 600},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
        assert [request.headers["chatgpt-account-id"] for request in public_calls] == ["account-0", "account-1"]
        successful_account = env.bindings[1].credential_id
        before_admin_ledger = await ledger_totals(pg_db)

        quota_calls = []
        reset_sent = False

        def admin_reply(request):
            nonlocal reset_sent
            quota_calls.append(request)
            if request.method == "POST":
                reset_sent = True
                return httpx.Response(204)
            if request.url.path.endswith("/usage"):
                return httpx.Response(200, json={
                    "plan_type": "plus",
                    "rate_limit": {
                        "primary_window": {"used_percent": 42, "limit_window_seconds": 18000},
                        "secondary_window": {"used_percent": 18, "limit_window_seconds": 604800},
                    },
                })
            available = 1 if reset_sent else 2
            credits = [{"id": "synthetic-credit-a", "state": "available", "expires_at": "2030-01-01T00:00:00Z"}]
            if not reset_sent:
                credits.append({"id": "synthetic-credit-b", "state": "available", "expires_at": "2030-01-01T00:00:00Z"})
            return httpx.Response(200, json={"available_count": available, "credits": credits})

        async with private_env(pg_db, settings, admin_reply) as (client, _):
            keys = (await client.get("/api/service/keys")).json()
            key = next(row for row in keys if row["key_id"] == str(env.key_id))
            # (1000-600)*2 + 600*1 + 85*3 = 1655 micro, not 1655 tokens.
            assert key["balance"] == {
                "total_micro": "100000000000000",
                "spent_micro": "1655",
                "held_micro": "0",
                "available_micro": "99999999998345",
            }
            account_view = await client.get("/api/service/oauth-accounts")
            assert account_view.status_code == 200
            assert any(row["id"] == str(successful_account) and row["email"] == "s***@e***.com"
                       for row in account_view.json()["items"])
            path = f"/api/service/oauth-accounts/{successful_account}"
            assert (await client.get(path + "/quota")).json()["snapshot"] is None
            assert not quota_calls, "storage-only account read must not call fake upstream"

            reports = (await client.get("/api/service/usage/accounts")).json()["items"]
            by_account = {row["credential_id"]: row for row in reports}
            assert by_account[str(env.bindings[0].credential_id)]["charged_micro"] == "0"
            assert by_account[str(successful_account)]["cached_read"] == "600"
            assert by_account[str(successful_account)]["charged_micro"] == "1655"
            attempts = (await client.get("/api/service/usage/requests")).json()["items"]
            assert len(attempts) == 2
            by_status = {row["status"]: row for row in attempts}
            assert set(by_status) == {"rejected", "completed"}
            assert by_status["completed"]["usage"]["cached_read"] == "600"
            assert by_status["rejected"]["charged_micro"] == "0"

            refreshed = await client.post(path + "/quota/refresh", json={})
            assert refreshed.status_code == 200, refreshed.text
            assert refreshed.json()["usage_status"] == "updated"
            assert refreshed.json()["credits_status"] == "updated"
            assert refreshed.json()["usage"]["snapshot"]["primary"]["window_seconds"] == 18000
            assert refreshed.json()["credits"]["snapshot"]["available_count"] == 2
            assert "synthetic-credit-a" not in refreshed.text
            assert len(quota_calls) == 2

            credits = (await client.get(path + "/reset-credits")).json()
            version = credits["version"]
            request_id = str(uuid4())
            command = {"request_id": request_id, "credits_version": version, "acknowledge": True}
            reset = await client.post(path + "/reset-credits/consume", json=command)
            assert reset.status_code == 200, reset.text
            assert reset.json()["operation_id"] == request_id
            assert reset.json()["state"] == "succeeded"
            assert sum(request.method == "POST" for request in quota_calls) == 1
            new_credits = await client.get(path + "/reset-credits")
            assert new_credits.json()["snapshot"]["available_count"] == 1
            assert new_credits.json()["active_reset"] is None
            calls_before_replay = len(quota_calls)
            replay = await client.post(path + "/reset-credits/consume", json=command)
            assert replay.status_code == 200 and replay.json() == reset.json()
            assert len(quota_calls) == calls_before_replay
            new_quota = (await client.get(path + "/quota")).json()
            assert new_quota["snapshot"]["version"] > refreshed.json()["usage"]["snapshot"]["version"]
            assert new_quota["stale"] is False
            assert before_admin_ledger == (1, 1655)
            assert await ledger_totals(pg_db) == before_admin_ledger


async def test_unknown_reset_never_silently_retries_or_clears(pg_db, settings):
    post_calls = 0

    def reply(request):
        nonlocal post_calls
        if request.method == "POST":
            post_calls += 1
            raise httpx.ReadError("synthetic transport drop")
        if request.url.path.endswith("/usage"):
            return httpx.Response(200, json={"plan_type": "plus"})
        return httpx.Response(200, json={
            "available_count": 1,
            "credits": [{"id": "synthetic-credit", "state": "available", "expires_at": "2030-01-01T00:00:00Z"}],
        })

    async with private_env(pg_db, settings, reply) as (client, services):
        identity = await account(services)
        path = f"/api/service/oauth-accounts/{identity}"
        await client.post(path + "/quota/refresh", json={})
        credits = (await client.get(path + "/reset-credits")).json()
        command = {"request_id": str(uuid4()), "credits_version": credits["version"], "acknowledge": True}
        first = await client.post(path + "/reset-credits/consume", json=command)
        assert first.status_code == 202 and first.json()["state"] == "unknown"
        replay = await client.post(path + "/reset-credits/consume", json=command)
        assert replay.status_code == 202 and replay.json() == first.json()
        assert post_calls == 1
        assert (await client.post(path + "/quota/refresh", json={})).status_code == 200
        assert (await client.get(path + "/reset-credits")).json()["active_reset"] == first.json()
        assert (await client.post(path + "/reset-credits/consume", json=dict(command, request_id=str(uuid4())))).status_code == 409
        assert (await client.get("/api/service/usage/requests")).json()["items"] == []
        assert await ledger_totals(pg_db) == (0, 0)


async def test_explicit_admin_refresh_finishes_confirmed_success_only_after_complete_snapshots(pg_db, settings):
    post_calls, failed = 0, set()

    def reply(request):
        nonlocal post_calls, failed
        if request.method == 'POST':
            post_calls += 1
            failed = {'usage', 'credits'}
            return httpx.Response(204)
        kind = 'usage' if request.url.path.endswith('/usage') else 'credits'
        if kind in failed:
            return httpx.Response(500)
        return httpx.Response(200, json={'plan_type':'plus'} if kind == 'usage' else {'available_count':2-post_calls})

    async with private_env(pg_db, settings, reply) as (client, services):
        identity = await account(services)
        path = f'/api/service/oauth-accounts/{identity}'
        initial = (await client.post(path+'/quota/refresh', json={})).json()
        request_id = uuid4()
        command = {'request_id':str(request_id), 'credits_version':initial['credits']['version'], 'acknowledge':True}
        response = await client.post(path+'/reset-credits/consume', json=command)
        assert response.status_code == 200 and response.json()['state'] == 'succeeded_refresh_failed'
        original = await services.quota_store.get_reset(request_id)
        for failures in ({'usage','credits'}, {'usage'}, {'credits'}):
            failed = failures
            refreshed = await client.post(path+'/quota/refresh', json={})
            assert refreshed.status_code == 200
            assert refreshed.json()['credits']['active_reset']['state'] == 'succeeded_refresh_failed'
            assert (await services.quota_store.get_reset(request_id)).state == 'succeeded_refresh_failed'
        failed = set()
        refreshed = await client.post(path+'/quota/refresh', json={})
        assert refreshed.status_code == 200
        assert refreshed.json()['usage_status'] == refreshed.json()['credits_status'] == 'updated'
        terminal = await services.quota_store.get_reset(request_id)
        assert terminal.state == 'succeeded'
        assert terminal.operation_id == original.operation_id and terminal.upstream_status == 204
        assert terminal.version == original.version+1
        assert refreshed.json()['credits']['active_reset'] is None
        assert refreshed.json()['credits']['last_reset']['state'] == 'succeeded'
        assert (await client.get(path+'/reset-credits')).json()['last_reset']['operation_id'] == str(request_id)
        assert post_calls == 1
        assert await ledger_totals(pg_db) == (0, 0)
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='codex.reset.succeeded'")) == 1


@pytest.mark.parametrize('fence', ['generation', 'stamp'])
async def test_explicit_refresh_cannot_finish_a_stale_reset_candidate(pg_db, settings, fence):
    posts, failing, invalidate = 0, False, False

    async def reply(request):
        nonlocal posts, failing, invalidate
        if request.method == 'POST':
            posts += 1
            failing = True
            return httpx.Response(204)
        if failing:
            return httpx.Response(500)
        if invalidate:
            invalidate = False
            async with pg_db.sessions.begin() as session:
                change = "generation=generation+1" if fence == 'generation' else "stamp='{}'::jsonb"
                await session.execute(text('UPDATE codex_reset_requests SET '+change))
        return httpx.Response(200, json={'plan_type':'plus'} if request.url.path.endswith('/usage') else {'available_count':1})

    async with private_env(pg_db, settings, reply) as (client, services):
        identity = await account(services)
        path = f'/api/service/oauth-accounts/{identity}'
        fresh = (await client.post(path+'/quota/refresh', json={})).json()
        consumed = await client.post(path+'/reset-credits/consume', json={'request_id':str(uuid4()), 'credits_version':fresh['credits']['version'], 'acknowledge':True})
        assert consumed.json()['state'] == 'succeeded_refresh_failed'
        failing, invalidate = False, True
        refreshed = await client.post(path+'/quota/refresh', json={})
        assert refreshed.status_code == 200
        assert refreshed.json()['usage']['stale'] is refreshed.json()['credits']['stale'] is False
        assert refreshed.json()['credits']['active_reset'] == consumed.json()
        assert posts == 1
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='codex.reset.succeeded'")) == 0


@pytest.mark.parametrize("query", [
    {"to": "0001-01-01T00:00:00Z"},
    {"to": "0001-01-01T00:00:00+01:00"},
    {"to": "9999-12-31T23:59:59-01:00"},
])
async def test_authenticated_usage_extreme_dates_are_safe_422(pg_db, settings, query):
    async with private_env(pg_db, settings) as (client, _):
        for endpoint in ("accounts", "requests"):
            response = await client.get("/api/service/usage/" + endpoint, params=query)
            assert response.status_code == 422, response.text
