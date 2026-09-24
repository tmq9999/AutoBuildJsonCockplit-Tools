import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from autobuild_json.gateway.accounts.imports import CODEX_PROVIDER
from autobuild_json.gateway.identity.policy import KeyPolicy
from autobuild_json.gateway.identity.service import IdentityService
from autobuild_json.gateway.routing.pool_records import AccountPoolPolicy, PoolMember
from autobuild_json.gateway.routing.pool_store import PoolStore
from autobuild_json.gateway.routing.records import BindingConfig, ModelConfig
from tests.gateway.harness import gateway_environment
from .test_oauth_import import record, credential_service
from .test_responses_gateway import native_wire

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
BODY = {"model": "codex-test", "input": "Hi"}


@asynccontextmanager
async def codex_environment(db, *, respond=None, pool=True):
    async with gateway_environment(db, respond=respond or (lambda r: httpx.Response(200, content=native_wire()))) as env:
        env.engine.digest_key = b"p" * 32
        service = await credential_service(db)
        env.engine.credentials = service
        catalog = env.engine.catalog
        await catalog.put_model(ModelConfig(model_id="codex-test", identity="codex-test", enabled=True,
                                             input_micro=2, output_micro=3, cache_read_micro=1, cache_write_micro=4))
        env.bindings = []
        for index in range(3):
            source = record()
            source["account"]["id"] = f"account-{index}"
            credential = await service.import_record(source, None)
            async with db.sessions.begin() as session:
                await session.execute(text("UPDATE credentials SET health='active',token_expires_at=now()+interval '1 hour' "
                                           "WHERE id=:id"), {"id": credential})
            binding = BindingConfig(UUID(int=index+1), CODEX_PROVIDER, credential, "codex-test", "codex-test",
                                    "codex-test", 1000+index*100, 500+index*100,
                                    frozenset({"text", "tools", "continuation"}), priority=index)
            env.bindings.append(binding)
            await catalog.put_binding(binding)
        env.policy = AccountPoolPolicy(mode="priority", members=tuple(
            PoolMember(credential_id=b.credential_id, priority=i) for i, b in enumerate(env.bindings)))
        env.pools = PoolStore(db, scope_key=b"p" * 32)
        if pool:
            await env.pools.put("codex-test", None, env.policy, "fixture")
        identity = IdentityService(db, b"p" * 32)
        await identity.update_policy(env.key_id, 1, KeyPolicy(model_ids={"codex-test"},
            protocols={"openai", "anthropic", "gemini", "ollama"}, total_micro=10**12, concurrency=10))
        env.principal = await identity.authenticate(env.secret, "openai")
        env.headers = {"Authorization": "Bearer " + env.secret}
        yield env


async def test_same_provider_distinct_credential_retry_is_attributed_and_settled_once(pg_db):
    def respond(request):
        if request.headers["chatgpt-account-id"] == "account-0":
            return httpx.Response(429, headers={"retry-after": "120"})
        return httpx.Response(200, content=native_wire())
    async with codex_environment(pg_db, respond=respond) as env:
        result = await env.client.post("/v1/responses", json=BODY, headers=env.headers)
        assert result.status_code == 200, result.text
        assert [r["headers"]["chatgpt-account-id"] for r in env.upstream_requests] == ["account-0", "account-1"]
        async with pg_db.sessions() as session:
            attempts = (await session.execute(text("SELECT * FROM attempts ORDER BY started_at"))).mappings().all()
            assert [a["credential_id"] for a in attempts] == [b.credential_id for b in env.bindings[:2]]
            assert [a["status"] for a in attempts] == ["rejected", "completed"]
            assert [a["provider_id"] for a in attempts] == [CODEX_PROVIDER, CODEX_PROVIDER]
            assert [a["binding_id"] for a in attempts] == [b.id for b in env.bindings[:2]]
            assert [a["config_version"] for a in attempts] == [1, 1]
            assert attempts[0]["usage"] is None
            assert attempts[1]["usage"]["input_tokens"] == 4
            assert "charged_micro" not in attempts[1]["usage"]
            assert (await session.execute(text("SELECT count(*),sum(amount_micro) FROM usage_ledger"))).one() == (1, 23)
            assert await session.scalar(text("SELECT cooldown_until FROM providers WHERE id=:id"), {"id": CODEX_PROVIDER}) is None
            assert await session.scalar(text("SELECT cooldown_until>now() FROM credentials WHERE id=:id"),
                                        {"id": env.bindings[0].credential_id})
            assert await session.scalar(text("SELECT cooldown_until FROM credentials WHERE id=:id"),
                                        {"id": env.bindings[1].credential_id}) is None
            failed = (await session.execute(text("SELECT last_failure_at,last_error,failure_count FROM codex_account_state "
                                                "WHERE credential_id=:id"), {"id": env.bindings[0].credential_id})).one()
            assert failed[0] is not None and failed[1:] == ("rate_limited", 1)
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0


async def test_pool_pause_precedes_customer_reservation(pg_db):
    async with codex_environment(pg_db) as env:
        await env.pools.put("codex-test", 1, AccountPoolPolicy(), "fixture")
        result = await env.client.post("/v1/responses", json=BODY, headers=env.headers)
        assert result.status_code == 503
        assert not env.upstream_requests
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM requests")) == 0


@pytest.mark.parametrize("native", [False, True])
async def test_pinned_session_or_continuation_never_retries(pg_db, native):
    rejected = False
    def respond(request):
        return httpx.Response(429, headers={"retry-after": "60"}) if rejected else httpx.Response(200, content=native_wire())
    async with codex_environment(pg_db, respond=respond) as env:
        headers = env.headers if native else dict(env.headers, **{"X-Gateway-Session": "private-session"})
        first = await env.client.post("/v1/responses", json=BODY, headers=headers)
        assert first.status_code == 200, first.text
        rejected = True
        body = dict(BODY, previous_response_id=first.json()["id"]) if native else BODY
        second = await env.client.post("/v1/responses", json=body, headers=headers)
        assert second.status_code == 429, second.text
        assert 58 <= int(second.headers["retry-after"]) <= 60
        assert [r["headers"]["chatgpt-account-id"] for r in env.upstream_requests] == ["account-0", "account-0"]
        assert all("x-gateway-session" not in r["headers"] for r in env.upstream_requests)


@pytest.mark.parametrize("value", ["", "x"*201, "bad\tvalue", "caf\xe9"])
async def test_session_header_rejects_invalid_values_before_dispatch(pg_db, value):
    async with codex_environment(pg_db) as env:
        headers = [(b"authorization", ("Bearer "+env.secret).encode()), (b"x-gateway-session", value.encode("latin1"))]
        result = await env.client.post("/v1/responses", json=BODY, headers=headers)
        assert result.status_code == 400
        assert not env.upstream_requests


async def test_session_header_rejects_duplicates(pg_db):
    async with codex_environment(pg_db) as env:
        headers = list(env.headers.items()) + [("x-gateway-session", "one"), ("x-gateway-session", "two")]
        result = await env.client.post("/v1/responses", json=BODY, headers=headers)
        assert result.status_code == 400
        assert not env.upstream_requests


async def test_legacy_no_pool_still_cools_only_rejected_account(pg_db):
    async with codex_environment(pg_db, pool=False, respond=lambda r: httpx.Response(429)) as env:
        result = await env.client.post("/v1/responses", json=BODY, headers=env.headers)
        assert result.status_code == 429
        assert [r["headers"]["chatgpt-account-id"] for r in env.upstream_requests] == ["account-0", "account-1"]
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM attempts")) == 2
            assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 1
            assert await session.scalar(text("SELECT sum(amount_micro) FROM usage_ledger")) == 0


async def test_header_affinity_survives_pool_reordering_and_native_conflict_rejects(pg_db):
    async with codex_environment(pg_db) as env:
        headers = dict(env.headers, **{"X-Gateway-Session": "private-session"})
        first = await env.client.post("/v1/responses", json=BODY, headers=headers)
        assert first.status_code == 200
        reordered = env.policy.model_copy(update={"members": tuple(
            PoolMember(credential_id=b.credential_id, priority=2-i) for i, b in enumerate(env.bindings))})
        await env.pools.put("codex-test", 1, reordered, "fixture")
        second = await env.client.post("/v1/responses", json=BODY, headers=headers)
        assert second.status_code == 200
        assert env.upstream_requests[-1]["headers"]["chatgpt-account-id"] == "account-0"
        unbound = await env.client.post("/v1/responses", json=BODY, headers=env.headers)
        assert unbound.status_code == 200
        assert env.upstream_requests[-1]["headers"]["chatgpt-account-id"] == "account-2"
        conflict = await env.client.post("/v1/responses", json=dict(BODY, previous_response_id=unbound.json()["id"]), headers=headers)
        assert conflict.status_code == 400 and conflict.json()["error"]["code"] == "invalid_state"
        assert len(env.upstream_requests) == 3
        async with pg_db.sessions() as session:
            row = (await session.execute(text("SELECT scope_digest,expires_at<=now()+interval '24 hours' FROM account_session_bindings"))).one()
            assert len(row[0]) == 32 and row[1]
            assert "private-session" not in str((await session.execute(text("SELECT * FROM account_session_bindings"))).all())


async def test_concurrent_first_header_requests_use_atomic_account_winner(pg_db):
    async with codex_environment(pg_db, pool=False) as env:
        # Equal-priority legacy ordering gives concurrent requests different candidates.
        for binding in env.bindings:
            await env.engine.catalog.put_binding(replace(binding, priority=0))
        headers = dict(env.headers, **{"X-Gateway-Session": "concurrent-private"})
        results = await asyncio.gather(*(env.client.post("/v1/responses", json=BODY, headers=headers) for _ in range(4)))
        assert [r.status_code for r in results] == [200]*4
        assert len({r["headers"]["chatgpt-account-id"] for r in env.upstream_requests}) == 1
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM account_session_bindings")) == 1


@pytest.mark.parametrize("mode", ["timeout", "partial", "missing_usage"])
async def test_ambiguous_or_started_generation_never_retries_or_refunds(pg_db, mode):
    def respond(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("synthetic")
        wire = native_wire()
        if mode == "partial":
            wire = wire.split(b"event: response.completed")[0]
        else:
            wire = wire.replace(b'"usage": {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9}', b'"usage": null')
        return httpx.Response(200, content=wire)
    async with codex_environment(pg_db, respond=respond) as env:
        response = await env.client.post("/v1/responses", json=BODY, headers=env.headers)
        assert response.status_code >= 400
        assert len(env.upstream_requests) == 1
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT state FROM requests")) == "usage_pending"
            assert await session.scalar(text("SELECT count(*) FROM attempts")) == 1
            assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 0
            assert await session.scalar(text("SELECT held FROM quota_buckets WHERE window_kind='total'")) == 5500
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0
            row = (await session.execute(text("SELECT last_error,failure_count,last_failure_at FROM codex_account_state "
                "WHERE credential_id=:id"), {"id": env.bindings[0].credential_id})).first()
            assert row and row[0] in {"deadline_exceeded", "upstream_error", "usage_pending"} and row[1] == 1 and row[2]


async def test_pool_retry_limit_and_single_mode_do_not_fall_through(pg_db):
    async with codex_environment(pg_db, respond=lambda r: httpx.Response(429)) as env:
        await env.pools.put("codex-test", 1, env.policy.model_copy(update={"retry_limit": 0}), "fixture")
        result = await env.client.post("/v1/responses", json=BODY, headers=env.headers)
        assert result.status_code == 429
        assert len(env.upstream_requests) == 1


async def test_success_and_failure_record_safe_metadata_without_resurrecting_health(pg_db):
    async with codex_environment(pg_db) as env:
        result = await env.client.post("/v1/responses", json=BODY, headers=env.headers)
        assert result.status_code == 200
        async with pg_db.sessions() as session:
            row = (await session.execute(text("SELECT * FROM codex_account_state WHERE credential_id=:id"),
                                         {"id": env.bindings[0].credential_id})).mappings().first()
            assert row and row["last_success_at"] is not None and row["failure_count"] == 0
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE credentials SET health='reauth_required' WHERE id=:id"),
                                  {"id": env.bindings[0].credential_id})
        await env.engine.catalog.credential_outcome(CODEX_PROVIDER, env.bindings[0].credential_id)
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT health FROM credentials WHERE id=:id"),
                                        {"id": env.bindings[0].credential_id}) == "reauth_required"


async def test_retry_keeps_customer_rates_and_resizes_codex_output_bound(pg_db, monkeypatch):
    async with codex_environment(pg_db, respond=lambda r: httpx.Response(429) if r.headers["chatgpt-account-id"] == "account-0"
                                 else httpx.Response(200, content=native_wire())) as env:
        cooldown = env.engine.catalog.cooldown
        async def update_rates(*args, **kwargs):
            await cooldown(*args, **kwargs)
            await env.engine.catalog.put_model(ModelConfig(model_id="codex-test", identity="codex-test", enabled=True,
                input_micro=200, output_micro=300, cache_read_micro=100, cache_write_micro=400))
        monkeypatch.setattr(env.engine.catalog, "cooldown", update_rates)
        result = await env.client.post("/v1/responses", json=BODY, headers=env.headers)
        assert result.status_code == 200, result.text
        async with pg_db.sessions() as session:
            assert (await session.execute(text("SELECT input_micro,output_micro,cache_read_micro,cache_write_micro,"
                                              "input_bound,output_bound FROM requests"))).one() == (2, 3, 1, 4, 1100, 600)
            assert await session.scalar(text("SELECT sum(amount_micro) FROM usage_ledger")) == 23


@pytest.mark.parametrize("mutation", ["key", "profile", "credential", "health", "model", "binding", "pool", "provider"])
async def test_mutation_while_attribution_waits_prevents_http_and_releases_hold(pg_db, monkeypatch, mutation):
    from pydantic import SecretStr
    from types import SimpleNamespace
    from autobuild_json.gateway.proxy.profiles import ProfileStore
    async with codex_environment(pg_db) as env:
        profiles = ProfileStore(pg_db, env.engine.vault, b"p"*32)
        payload = SimpleNamespace(name="fixture", mode="fixed", entries_text=SecretStr("http://proxy.invalid:8080"),
                                  region="random", protocol="http", rotate=False)
        profile = await profiles.create(payload)
        env.engine.proxy_resolver = profiles.load
        env.engine.credentials.profile_resolver = profiles.load
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE credentials SET profile_id=:profile WHERE id=:id"),
                                  {"profile": profile, "id": env.bindings[0].credential_id})
        ready, release = asyncio.Event(), asyncio.Event()
        mark = env.ledger.mark_dispatched
        async def gate(*args, **kwargs):
            await mark(*args, **kwargs)
            ready.set()
            await release.wait()
        monkeypatch.setattr(env.ledger, "mark_dispatched", gate)
        task = asyncio.create_task(env.client.post("/v1/responses", json=BODY, headers=env.headers))
        try:
            await asyncio.wait_for(ready.wait(), 5)
            if mutation == "key":
                await IdentityService(pg_db, b"p"*32).update_policy(env.key_id, 2, KeyPolicy(model_ids={"elsewhere"}))
            elif mutation == "profile":
                await profiles.update(profile, 1, payload)
            elif mutation == "credential":
                await env.engine.catalog.set_credential_enabled(env.bindings[0].credential_id, False)
            elif mutation == "health":
                async with pg_db.sessions.begin() as session:
                    await session.execute(text("UPDATE credentials SET health='reauth_required' WHERE id=:id"),
                                          {"id": env.bindings[0].credential_id})
            elif mutation == "model":
                await env.engine.catalog.put_model(ModelConfig(model_id="codex-test", identity="codex-test", enabled=False))
            elif mutation == "binding":
                await env.engine.catalog.put_binding(replace(env.bindings[0], upstream_model="changed"))
            elif mutation == "pool":
                await env.pools.put("codex-test", 1, AccountPoolPolicy(), "fixture")
            else:
                async with pg_db.sessions.begin() as session:
                    await session.execute(text("UPDATE providers SET version=version+1 WHERE id=:id"), {"id": CODEX_PROVIDER})
            release.set()
            result = await asyncio.wait_for(task, 8)
            assert result.status_code >= 400
            assert not env.upstream_requests
            async with pg_db.sessions() as session:
                assert await session.scalar(text("SELECT state FROM requests")) == "released"
                assert await session.scalar(text("SELECT sum(amount_micro) FROM usage_ledger")) == 0
                assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0
                assert await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL")) == 0
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_external_first_mixed_router_ignores_codex_header_affinity(pg_db):
    from tests.gateway.harness import chat_success
    from autobuild_json.gateway.routing.records import ProviderConfig
    async with codex_environment(pg_db, pool=False, respond=lambda r: httpx.Response(200, content=chat_success())) as env:
        await env.engine.catalog.put_model(ModelConfig(model_id="codex-test", identity="codex-test", router_model=True, enabled=True))
        provider = await env.engine.catalog.create_provider(ProviderConfig(name="external", adapter="openai_compatible",
                                                                          root="https://external.invalid/v1"))
        credential = await env.engine.catalog.put_credential(provider, "synthetic")
        await env.engine.catalog.put_binding(replace(env.bindings[0], id=UUID(int=0), provider_id=provider, credential_id=credential))
        result = await env.client.post("/v1/responses", json=BODY,
                                       headers=dict(env.headers, **{"X-Gateway-Session": "external-session"}))
        assert result.status_code == 200, result.text
        assert env.upstream_requests[0]["url"].startswith("https://external.invalid/")
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM account_session_bindings")) == 0


async def test_existing_codex_header_pin_survives_external_first_router_change(pg_db):
    from autobuild_json.gateway.routing.records import ProviderConfig
    async with codex_environment(pg_db, pool=False) as env:
        headers = dict(env.headers, **{"X-Gateway-Session": "keep-account"})
        first = await env.client.post("/v1/responses", json=BODY, headers=headers)
        assert first.status_code == 200
        await env.engine.catalog.put_model(ModelConfig(model_id="codex-test", identity="codex-test", router_model=True, enabled=True))
        provider = await env.engine.catalog.create_provider(ProviderConfig(name="external", adapter="openai_compatible",
            wire_api="responses", root="https://external.invalid/v1"))
        credential = await env.engine.catalog.put_credential(provider, "synthetic")
        await env.engine.catalog.put_binding(replace(env.bindings[0], id=UUID(int=0), provider_id=provider, credential_id=credential))
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE public_models SET cursor=0 WHERE id='codex-test'"))
        second = await env.client.post("/v1/responses", json=BODY, headers=headers)
        assert second.status_code == 200, second.text
        assert [r["headers"].get("chatgpt-account-id") for r in env.upstream_requests] == ["account-0", "account-0"]


@pytest.mark.parametrize("writer_kind", ["pool", "binding", "affinity"])
async def test_dispatch_attribution_fk_locks_precede_accounting_and_configuration_writers(pg_db, monkeypatch, writer_kind):
    from sqlalchemy.ext.asyncio import AsyncSession
    from autobuild_json.gateway.metering.records import Admission, Bounds
    from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec
    from autobuild_json.gateway.routing.pool_records import PoolScope
    from .test_account_pool import wait_for_blocker
    async with codex_environment(pg_db) as env:
        route = (await env.engine.catalog.candidates(env.principal, ResponsesCodec().decode(BODY, {})))[0]
        deadline = datetime.now(timezone.utc)+timedelta(seconds=60)
        request_id = uuid4()
        await env.ledger.reserve(Admission(request_id, env.principal, "codex-test", Bounds(1000, 500), 2, 3, deadline))
        ready, release, writer_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
        pids = {}
        original_key, original_execute, original_scalar = env.ledger._lock_key, AsyncSession.execute, AsyncSession.scalar
        async def gate_key(session, key_id):
            result = await original_key(session, key_id)
            pids["dispatch"] = await session.scalar(text("SELECT pg_backend_pid()"))
            ready.set()
            await release.wait()
            return result
        async def observe(session, statement, *args, **kwargs):
            if asyncio.current_task().get_name() == "dispatch-config-writer" and "writer" not in pids:
                pids["writer"] = (await original_execute(session, text("SELECT pg_backend_pid()"))).scalar_one()
                writer_ready.set()
            return await original_execute(session, statement, *args, **kwargs)
        async def observe_scalar(session, statement, *args, **kwargs):
            if asyncio.current_task().get_name() == "dispatch-config-writer" and "writer" not in pids:
                pids["writer"] = (await original_execute(session, text("SELECT pg_backend_pid()"))).scalar_one()
                writer_ready.set()
            return await original_scalar(session, statement, *args, **kwargs)
        monkeypatch.setattr(env.ledger, "_lock_key", gate_key)
        monkeypatch.setattr(AsyncSession, "execute", observe)
        monkeypatch.setattr(AsyncSession, "scalar", observe_scalar)
        dispatch = asyncio.create_task(env.ledger.mark_dispatched(request_id, uuid4(), route=route))
        writer = None
        try:
            await asyncio.wait_for(ready.wait(), 5)
            if writer_kind == "pool":
                operation = env.pools.put("codex-test", 1, env.policy, "fixture")
            elif writer_kind == "binding":
                operation = env.engine.catalog.put_binding(env.bindings[0])
            else:
                scope = PoolScope(customer_id=env.principal.customer_id, key_id=env.key_id,
                                  model_id="codex-test", session_digest=b"s"*32)
                operation = env.pools.bind(scope, route, deadline)
            writer = asyncio.create_task(operation, name="dispatch-config-writer")
            await asyncio.wait_for(writer_ready.wait(), 5)
            await wait_for_blocker(pg_db, pids["writer"], pids["dispatch"])
            release.set()
            await asyncio.wait_for(asyncio.gather(dispatch, writer), 8)
            async with pg_db.sessions() as session:
                assert (await session.execute(text("SELECT provider_id,credential_id,binding_id FROM attempts"))).one() == (
                    route.provider_id, route.credential_id, route.binding_id)
        finally:
            release.set()
            for task in (dispatch, writer):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (dispatch, writer) if t is not None), return_exceptions=True)


async def test_retry_uses_selected_account_proxy_and_no_quota_http(pg_db, monkeypatch):
    from pydantic import SecretStr
    from types import SimpleNamespace
    from autobuild_json.gateway.proxy.profiles import ProfileStore
    from autobuild_json.gateway.transport.http import CoreTransport
    calls = []
    async def http_boundary(self, request):
        calls.append((request, self))
        return httpx.Response(429, headers={"retry-after": "60"}) if len(calls) == 1 else httpx.Response(200, content=native_wire())
    monkeypatch.setattr(CoreTransport, "handle_async_request", http_boundary)
    async with codex_environment(pg_db) as env:
        profiles = ProfileStore(pg_db, env.engine.vault, b"p"*32)
        env.engine.proxy_resolver = profiles.load
        env.engine.credentials.profile_resolver = profiles.load
        profile = await profiles.create(SimpleNamespace(name="proxy", mode="fixed", entries_text=SecretStr("http://proxy.invalid:8080"),
            region="random", protocol="http", rotate=False))
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE credentials SET profile_id=:profile WHERE id=:id"),
                                  {"profile": profile, "id": env.bindings[1].credential_id})
        env.engine.transport.adapter = None
        result = await env.client.post("/v1/responses", json=BODY, headers=env.headers)
        assert result.status_code == 200, result.text
        import httpcore
        assert type(calls[0][1].pool) is httpcore.AsyncConnectionPool
        assert type(calls[1][1].pool) is httpcore.AsyncHTTPProxy
        assert calls[1][1].pool._proxy_url.host == b"proxy.invalid"
        assert [r.headers["chatgpt-account-id"] for r, _ in calls] == ["account-0", "account-1"]
        assert [(r.method, r.url.path) for r, _ in calls] == [("POST", "/backend-api/codex/responses")]*2
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL")) == 0


async def test_codex_retry_after_starts_before_response_close(pg_db):
    from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec
    entered, release = asyncio.Event(), asyncio.Event()
    class Rejection(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b""
        async def aclose(self):
            entered.set()
            await release.wait()
    async with codex_environment(pg_db, respond=lambda r: httpx.Response(429, headers={"retry-after": "1"}, stream=Rejection())) as env:
        await env.pools.put("codex-test", 1, env.policy.model_copy(update={"retry_limit": 0}), "fixture")
        task = asyncio.create_task(env.engine.prepare(env.principal, ResponsesCodec().decode(BODY, {}), env.engine.meta(BODY)))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            # Time intentionally passes at the external response-close boundary.
            await asyncio.sleep(1.1)
            release.set()
            from autobuild_json.gateway.errors import GatewayError
            with pytest.raises(GatewayError):
                await task
            async with pg_db.sessions() as session:
                assert await session.scalar(text("SELECT cooldown_until<=clock_timestamp() FROM credentials WHERE id=:id"),
                                            {"id": env.bindings[0].credential_id})
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_repeated_cancel_during_preparation_joins_accounting_cleanup(pg_db, monkeypatch):
    from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec
    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async with codex_environment(pg_db) as env:
        async def http_boundary(request):
            entered.set()
            await asyncio.Event().wait()
        env.engine.transport.adapter = httpx.MockTransport(http_boundary)
        pending = env.ledger.mark_pending
        async def gated_pending(*args):
            cleanup.set()
            await release.wait()
            await pending(*args)
        monkeypatch.setattr(env.ledger, "mark_pending", gated_pending)
        task = asyncio.create_task(env.engine.prepare(env.principal, ResponsesCodec().decode(BODY, {}), env.engine.meta(BODY)))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            await asyncio.wait_for(cleanup.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with pg_db.sessions() as session:
                assert await session.scalar(text("SELECT state FROM requests")) == "usage_pending"
                assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 0
                assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0
                assert await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL")) == 0
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_codex_stream_cancel_keeps_hold_and_closes_response(pg_db):
    from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec
    async with codex_environment(pg_db) as env:
        prepared = await env.engine.prepare(env.principal, ResponsesCodec().decode(BODY, {}), env.engine.meta(BODY))
        events = prepared.events()
        first = await events.__anext__()
        assert first.kind == "started" and first.response_id.startswith("resp_")
        await events.aclose()
        assert prepared.closed and prepared.stream.response.raw.is_closed
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT state FROM requests")) == "usage_pending"
            assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 0
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0


@pytest.mark.parametrize("cancelled", [False, True])
async def test_preparation_cleanup_hang_is_bounded_and_joined(pg_db, monkeypatch, cancelled):
    from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec
    from autobuild_json.gateway.errors import GatewayError
    entered, cleanup, joined, finished = (asyncio.Event() for _ in range(4))
    async with codex_environment(pg_db) as env:
        async def http_boundary(request):
            entered.set()
            if cancelled:
                await asyncio.Event().wait()
            raise httpx.ReadTimeout("synthetic")
        env.engine.transport.adapter = httpx.MockTransport(http_boundary)
        async def stuck(*args):
            cleanup.set()
            try:
                await asyncio.Event().wait()
            finally:
                joined.set()
        monkeypatch.setattr(env.ledger, "mark_pending", stuck)
        async def invoke():
            try:
                await env.engine.prepare(env.principal, ResponsesCodec().decode(BODY, {}), env.engine.meta(BODY))
            finally:
                finished.set()
        task = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if cancelled:
                task.cancel()
            await asyncio.wait_for(cleanup.wait(), 5)
            if cancelled:
                task.cancel()
            # Observe return without cancelling the work under test on timeout.
            done, _ = await asyncio.wait({task}, timeout=6)
            assert task in done, "cleanup exceeded its 5 second budget"
            if cancelled:
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(GatewayError, match="deadline_exceeded"):
                    await task
            assert joined.is_set() and finished.is_set()
            async with pg_db.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0
        finally:
            if not task.done():
                # RED tree has an unbounded owned cleanup task. Cancel only
                # the task identifiable by this test's stuck coroutine frame.
                for child in asyncio.all_tasks():
                    if child is not asyncio.current_task() and child is not task:
                        frames = child.get_stack()
                        if any(frame.f_code.co_name == "cleanup" and frame.f_code.co_filename.endswith("/gateway/engine.py") for frame in frames):
                            child.cancel()
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
