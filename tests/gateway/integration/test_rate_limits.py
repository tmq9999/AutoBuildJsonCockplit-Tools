from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from autobuild_json.gateway.identity.policy import KeyPolicy
from autobuild_json.gateway.identity.service import IdentityService
from autobuild_json.gateway.routing.records import ProviderConfig
from tests.gateway.harness import chat_success, gateway_environment
from .test_gateway import BODY

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def add_backup(env, *, priority=1):
    catalog = env.engine.catalog
    provider = await catalog.create_provider(ProviderConfig(
        name="Backup", adapter="openai_compatible", root=f"https://backup-{priority}.invalid/v1"))
    credential = await catalog.put_credential(provider, "synthetic-backup")
    await catalog.put_binding(replace(env.binding, id=uuid4(), provider_id=provider,
                                     credential_id=credential, priority=priority))
    return provider


@pytest.mark.parametrize("hints,expected", [
    (("10", "120"), "10"), (("120", "10"), "10"),
    ((None, "120"), None), (("bad", "120"), None), (("120", None), None),
    (("0", "120"), "0"),
])
async def test_exhausted_chain_uses_earliest_known_hint_not_last_provider(pg_db, hints, expected):
    responses = iter(hints)

    def respond(request):
        hint = next(responses)
        return httpx.Response(429, headers={"Retry-After": hint} if hint is not None else {},
                              json={"error": "private-upstream-body"})

    async with gateway_environment(pg_db, respond=respond) as env:
        await add_backup(env)
        response = await env.client.post("/v1/chat/completions", json=BODY,
                                         headers={"Authorization": "Bearer " + env.secret})
        assert response.status_code == 429
        assert response.headers.get("retry-after") == expected
        assert response.json()["error"]["code"] == "rate_limited"
        assert "private-upstream-body" not in response.text
        assert len(env.upstream_requests) == 2
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT state FROM requests")) == "released"
            assert await session.scalar(text("SELECT sum(amount_micro) FROM usage_ledger")) == 0
            assert await session.scalar(text("SELECT sum(held) FROM quota_buckets")) == 0
            assert await session.scalar(text("SELECT count(*) FROM attempts WHERE status='rejected'")) == 2
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0


async def test_attempt_limit_does_not_claim_unknown_candidates_are_rate_limited(pg_db):
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(429, headers={"Retry-After": "120"})) as env:
        await add_backup(env)
        await add_backup(env, priority=2)
        response = await env.client.post("/v1/chat/completions", json=BODY,
                                         headers={"Authorization": "Bearer " + env.secret})
        assert response.status_code == 429
        assert "retry-after" not in response.headers
        assert len(env.upstream_requests) == 2


@pytest.mark.parametrize("last_status", [200, 401, 500])
async def test_other_outcomes_do_not_inherit_a_failed_providers_retry_header(pg_db, last_status):
    responses = iter([httpx.Response(429, headers={"Retry-After": "120"}),
                      httpx.Response(last_status, content=chat_success() if last_status == 200 else b"private-body")])
    async with gateway_environment(pg_db, respond=lambda r: next(responses)) as env:
        await add_backup(env)
        response = await env.client.post("/v1/chat/completions", json=BODY,
                                         headers={"Authorization": "Bearer " + env.secret})
        assert response.status_code == (200 if last_status == 200 else 502)
        assert "retry-after" not in response.headers
        assert len(env.upstream_requests) == 2
        async with pg_db.sessions() as session:
            state = await session.scalar(text("SELECT state FROM requests"))
            assert state == {200: "completed", 401: "released", 500: "usage_pending"}[last_status]


@pytest.mark.parametrize("client", ["openai", "responses", "anthropic", "gemini", "ollama"])
@pytest.mark.parametrize("stream", [False, True])
async def test_rate_limit_uses_client_wire_error_before_any_stream(pg_db, client, stream):
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(
            429, headers={"Retry-After": "120", "X-Upstream-Secret": "private-header"})) as env:
        protocol = "openai" if client == "responses" else client
        await IdentityService(pg_db, b"p" * 32).update_policy(env.key_id, 1, KeyPolicy(
            model_ids={"public"}, protocols={protocol}, total_micro=10**12))
        headers = {"Authorization": "Bearer " + env.secret}
        if client == "responses":
            path, body = "/v1/responses", {"model": "public", "input": "hello", "stream": stream, "max_output_tokens": 8}
        elif client == "anthropic":
            path, body = "/v1/messages", dict(BODY, stream=stream)
            headers["anthropic-version"] = "2023-06-01"
        elif client == "gemini":
            path = "/v1beta/models/public:" + ("streamGenerateContent?alt=sse" if stream else "generateContent")
            body = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}],
                    "generationConfig": {"maxOutputTokens": 8}}
        elif client == "ollama":
            path, body = "/api/chat", {"model": "public", "messages": BODY["messages"], "stream": stream,
                                       "options": {"num_predict": 8}}
        else:
            path, body = "/v1/chat/completions", dict(BODY, stream=stream)
        response = await env.client.post(path, json=body, headers=headers)
        assert response.status_code == 429
        assert response.headers["retry-after"] == "120"
        assert response.headers["content-type"].startswith("application/json")
        assert "x-upstream-secret" not in response.headers
        error = response.json()["error"]
        if client == "anthropic":
            assert error["type"] == "rate_limit_error"
        elif client == "gemini":
            assert error["status"] == "RESOURCE_EXHAUSTED" and error["code"] == 429
        elif client == "ollama":
            assert error == "rate_limited"
        else:
            assert error["code"] == "rate_limited" and error["stage"] == "upstream"


async def test_zero_delay_does_not_create_an_invented_minute_cooldown(pg_db):
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(429, headers={"Retry-After": "0"})) as env:
        response = await env.client.post("/v1/chat/completions", json=BODY,
                                         headers={"Authorization": "Bearer " + env.secret})
        assert response.status_code == 429 and response.headers["retry-after"] == "0"
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT cooldown_until <= clock_timestamp() FROM providers"))


async def test_rejected_generation_releases_upstream_money_hold(pg_db):
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(429, headers={"Retry-After": "120"})) as env:
        budget = await env.engine.budgets.create("USD", Decimal("1"))
        await env.engine.catalog.update_provider(env.provider_id, 1, ProviderConfig(
            name="Budgeted", adapter="openai_compatible", root="https://provider.invalid/v1", budget_id=budget,
            cost_schedule={"currency": "USD", "input_per_million": "1", "output_per_million": "3"}))
        response = await env.client.post("/v1/chat/completions", json=BODY,
                                         headers={"Authorization": "Bearer " + env.secret})
        assert response.status_code == 429
        assert await env.engine.budgets.balance(budget) == (Decimal(0), Decimal(0), Decimal(1))


@pytest.mark.parametrize("adapter", ["anthropic", "gemini"])
async def test_native_token_count_rejection_honors_backoff_without_pending_usage(pg_db, adapter):
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(429, headers={"Retry-After": "120"})) as env:
        await IdentityService(pg_db, b"p" * 32).update_policy(env.key_id, 1, KeyPolicy(
            model_ids={"public"}, protocols={adapter}, total_micro=10**12))
        await env.engine.catalog.update_provider(env.provider_id, 1, ProviderConfig(
            name="Counter", adapter=adapter, root="https://provider.invalid/v1"))
        headers = {"Authorization": "Bearer " + env.secret}
        if adapter == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
            path, body = "/v1/messages/count_tokens", {"model": "public", "messages": BODY["messages"]}
        else:
            path = "/v1beta/models/public:countTokens"
            body = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
        response = await env.client.post(path, json=body, headers=headers)
        assert response.status_code == 429 and response.headers["retry-after"] == "120"
        assert len(env.upstream_requests) == 1
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT state FROM requests")) == "released"
            assert await session.scalar(text("SELECT status FROM attempts")) == "rejected"
            assert await session.scalar(text("SELECT sum(amount_micro) FROM usage_ledger")) == 0
            assert await session.scalar(text("SELECT cooldown_until > clock_timestamp() FROM providers"))


@pytest.mark.parametrize("mode", ["fixed", "pool", "kiotproxy"])
async def test_rate_limit_does_not_rotate_proxy_and_releases_its_leases(pg_db, mode):
    from pydantic import SecretStr
    from types import SimpleNamespace
    from autobuild_json.gateway.proxy.profiles import ProfileStore
    from autobuild_json.gateway.proxy.kiot import KiotClient
    from tests.gateway.unit.test_kiot import success

    controls = []

    def kiot_response(request):
        controls.append(request.url.path)
        return httpx.Response(200, json=success())

    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(429, headers={"Retry-After": "120"})) as env:
        profiles = ProfileStore(pg_db, env.engine.vault, b"p" * 32)
        entries = {"fixed": "http://93.184.216.34:39008",
                   "pool": "http://93.184.216.34:39008\nhttp://93.184.216.34:39009",
                   "kiotproxy": "synthetic-kiot-key"}[mode]
        profile = await profiles.create(SimpleNamespace(name="Proxy", mode=mode, entries_text=SecretStr(entries),
                                                        region="random", protocol="http", rotate=False))
        env.engine.proxy_resolver = profiles.load
        env.engine.proxies.kiot = KiotClient(adapter=httpx.MockTransport(kiot_response))
        await env.engine.catalog.update_provider(env.provider_id, 1, ProviderConfig(
            name="Proxied", adapter="openai_compatible", root="https://provider.invalid/v1", proxy_profile_id=profile))
        response = await env.client.post("/v1/chat/completions", json=BODY,
                                         headers={"Authorization": "Bearer " + env.secret})
        assert response.status_code == 429
        assert len(env.upstream_requests) == 1
        assert controls == (["/api/v1/proxies/current"] if mode == "kiotproxy" else [])
        async with pg_db.sessions() as session:
            rows = (await session.execute(text("SELECT owner,generation FROM proxy_leases"))).all()
            assert len(rows) == (2 if mode == "kiotproxy" else 1)
            assert all(owner is None and generation == 1 for owner, generation in rows)
            assert await session.scalar(text("SELECT sum(held) FROM quota_buckets")) == 0


async def test_backoff_includes_a_route_that_was_already_cooling(pg_db):
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(429, headers={"Retry-After": "120"})) as env:
        backup = await add_backup(env)
        await env.engine.catalog.cooldown(backup, None, 10)
        response = await env.client.post("/v1/chat/completions", json=BODY,
                                         headers={"Authorization": "Bearer " + env.secret})
        assert response.status_code == 429
        assert 8 <= int(response.headers["retry-after"]) <= 10
        assert len(env.upstream_requests) == 1


async def test_response_honors_longer_cooldown_written_by_another_worker(pg_db, monkeypatch):
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(429, headers={"Retry-After": "10"})) as env:
        reject = env.ledger.mark_rejected

        async def concurrent_rejection(*args):
            await reject(*args)
            await env.engine.catalog.cooldown(env.provider_id, None, 120)

        monkeypatch.setattr(env.ledger, "mark_rejected", concurrent_rejection)
        response = await env.client.post("/v1/chat/completions", json=BODY,
                                         headers={"Authorization": "Bearer " + env.secret})
        assert response.status_code == 429
        assert 118 <= int(response.headers["retry-after"]) <= 120


@pytest.mark.parametrize("adapter", ["openai_compatible", "anthropic", "gemini"])
async def test_slow_rejection_cleanup_does_not_restart_cooldown(pg_db, monkeypatch, adapter):
    from types import SimpleNamespace
    from autobuild_json.gateway import engine as engine_module
    from autobuild_json.gateway.providers import catalog as catalog_module
    clock = [100.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(engine_module, "time", fake_time)
    # The engine/catalog clock seam models elapsed cleanup time without sleeping.
    monkeypatch.setattr(catalog_module, "time", fake_time, raising=False)
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(429, headers={"Retry-After": "1"})) as env:
        headers = {"Authorization": "Bearer " + env.secret}
        path, body = "/v1/chat/completions", BODY
        if adapter != "openai_compatible":
            await IdentityService(pg_db, b"p" * 32).update_policy(env.key_id, 1, KeyPolicy(
                model_ids={"public"}, protocols={adapter}, total_micro=10**12))
            await env.engine.catalog.update_provider(env.provider_id, 1, ProviderConfig(
                name="Counter", adapter=adapter, root="https://provider.invalid/v1"))
            if adapter == "anthropic":
                path, body = "/v1/messages/count_tokens", {"model": "public", "messages": BODY["messages"]}
                headers["anthropic-version"] = "2023-06-01"
            else:
                path = "/v1beta/models/public:countTokens"
                body = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
        reject = env.ledger.mark_rejected

        async def slow_cleanup(*args):
            await reject(*args)
            clock[0] += 2

        monkeypatch.setattr(env.ledger, "mark_rejected", slow_cleanup)
        response = await env.client.post(path, json=body, headers=headers)
        assert response.status_code == 429 and response.headers["retry-after"] == "0"
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT cooldown_until <= clock_timestamp() FROM providers"))
