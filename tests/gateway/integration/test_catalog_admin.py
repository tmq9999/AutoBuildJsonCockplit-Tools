"""Private catalog boundary: wire conversion, durable commands and safe views."""

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from tests.gateway.integration.test_admin import admin_env

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
AUTH = {"x-test-session": "synthetic-session", "x-csrf-token": "synthetic-csrf"}


async def create_source(client):
    provider = (await client.post("/api/service/providers", json={"name": "Synthetic",
        "adapter": "openai_compatible", "root": "https://provider.invalid/v1"})).json()["id"]
    credential = (await client.post(f"/api/service/providers/{provider}/credentials",
                                    json={"secret": "synthetic-secret"})).json()["id"]
    body = {"provider_id": provider, "credential_id": credential, "mode": "openai_single"}
    response = await client.post("/api/service/catalog/sources", json=body)
    assert response.status_code == 201, response.text
    return response.json(), body


async def test_catalog_api_stays_private_and_csrf_protected(pg_db):
    async with admin_env(pg_db) as (client, services):
        assert (await client.get("/api/service/provider-presets")).status_code == 401
        client.headers["x-test-session"] = "synthetic-session"
        assert (await client.post("/api/service/catalog/sources", json={})).status_code == 403
        client.headers.update(AUTH)
        response = await client.get("/api/service/provider-presets")
        assert response.status_code == 200
        assert len(response.json()["presets"]) == 5
        assert response.headers["cache-control"] == "no-store"
        from autobuild_json.gateway.http.app import create_gateway_app
        public = create_gateway_app(services.identity, services.catalog, services.engine,
                                    allowed_hosts=("public",))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="http://public") as other:
            assert (await other.get("/api/service/provider-presets")).status_code == 404
            assert (await other.get("/api/service/catalog/sources")).status_code == 404


async def test_disabled_source_is_listable_and_editable_without_network(pg_db):
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        source, body = await create_source(client)
        identity = source["id"]
        changed = await client.put(f"/api/service/catalog/sources/{identity}",
                                   json=dict(body, version=1, enabled=False))
        assert changed.status_code == 200, changed.text
        listed = await client.get("/api/service/catalog/sources", params={"provider_id": body["provider_id"]})
        row = listed.json()["sources"][0]
        assert row["enabled"] is False and row["version"] == 2
        assert row["expected"]["source_version"] == 2
        assert row["recent_job"] is None and row["worker_available"] is False
        assert "secret" not in listed.text and "provider.invalid" not in listed.text
        blocked = await client.post(f"/api/service/catalog/sources/{identity}/discover",
            json={"operation_id": str(uuid4()), "expected": row["expected"]})
        assert blocked.status_code == 409
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM provider_operations")) == 0
            assert await session.scalar(text("SELECT count(*) FROM requests")) == 0
        assert (await client.put(f"/api/service/catalog/sources/{identity}",
            json=dict(body, version=2, enabled=True))).status_code == 200


async def test_enqueue_replay_alias_cancel_and_audit_once(pg_db):
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        source, _ = await create_source(client)
        row = (await client.get("/api/service/catalog/sources")).json()["sources"][0]
        command = {"operation_id": str(uuid4()), "expected": row["expected"]}
        path = f"/api/service/catalog/sources/{source['id']}/discover"
        response = await client.post(path, json=command)
        assert response.status_code == 202, response.text
        job = response.json()
        assert job["state"] == "queued"
        assert (await client.post(path, json=command)).status_code == 200
        alias = dict(command, operation_id=str(uuid4()))
        assert (await client.post(path, json=alias)).json()["id"] == job["id"]
        assert (await client.get(f"/api/service/provider-operations/{alias['operation_id']}")).json()["id"] == job["id"]
        assert (await client.post(path, json=dict(command, actor="DO-NOT-ECHO"))).status_code == 422
        cancellation = f"/api/service/provider-operations/{job['id']}/cancel"
        cancelled = await client.post(cancellation, json={"version": job["version"]})
        assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
        assert (await client.post(cancellation, json={"version": cancelled.json()["version"]})).status_code == 200
        async with pg_db.sessions() as session:
            rows = (await session.execute(text("SELECT action,actor,details FROM audit_events WHERE action LIKE 'catalog.operation.%' ORDER BY action"))).mappings().all()
        assert [(r["action"], r["actor"]) for r in rows] == [
            ("catalog.operation.cancelled", "admin"), ("catalog.operation.enqueued", "admin")]
        assert all("expected" not in r["details"] for r in rows)


async def test_async_job_failure_is_stored_not_an_admin_auth_failure(pg_db):
    from autobuild_json.gateway.transport.http import Transport
    from autobuild_json.gateway.transport.egress import EgressPolicy
    async def resolver(host, port):
        return ["93.184.216.34"]
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        services.transport = Transport(EgressPolicy(resolver=resolver), adapter=httpx.MockTransport(
            lambda request: httpx.Response(401, text="synthetic-private-body")))
        source, _ = await create_source(client)
        expected = (await client.get("/api/service/catalog/sources")).json()["sources"][0]["expected"]
        response = await client.post(f"/api/service/catalog/sources/{source['id']}/discover",
                                    json={"operation_id": str(uuid4()), "expected": expected})
        job = response.json()
        await services.operation_runner.run(UUID(job["id"]))
        view = await client.get(f"/api/service/provider-operations/{job['id']}")
        assert view.status_code == 200
        assert view.json()["state"] == "failed" and view.json()["error"] == "upstream_error"
        assert "synthetic-private-body" not in view.text
        assert (await client.get("/api/service/provider-presets")).status_code == 200


async def test_real_admin_session_catalog_queries_and_csrf(pg_db, settings):
    from autobuild_json.api import create_app
    from autobuild_json.gateway.admin.services import AdminServices
    from autobuild_json.results import RunStore
    from autobuild_json.runner import Runner
    from tests.gateway.catalog_support import synthetic_vault
    services = AdminServices(pg_db, synthetic_vault(), b"p" * 32)
    runner = Runner(RunStore(settings.data_dir / "runs"), lambda *args: None)
    app = create_app(settings, runner=runner, gateway_services=services)
    origin = "http://127.0.0.1:8787"
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
            assert (await client.get("/api/service/catalog/sources", params={"provider_id": str(uuid4())})).status_code == 401
            login = await client.post("/api/session", headers={"Origin": origin}, json={"token": "test-admin-token"})
            assert login.status_code == 200, login.text
            client.headers["Origin"] = origin
            assert (await client.post("/api/service/catalog/sources", json={})).status_code == 403
            client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            source, body = await create_source(client)
            assert (await client.get("/api/service/catalog/sources", params={"provider_id": body["provider_id"]})).status_code == 200
            path = f"/api/service/catalog/sources/{source['id']}"
            assert (await client.get(path + "/entries?limit=1&diff=added")).status_code == 200
            assert (await client.get(path + "/probe-quote", params={"binding_id": str(uuid4())})).status_code == 404
            for query in ("token=DO-NOT-ECHO", "provider_id=x&provider_id=y", "limit=1", "provider_id=%00"):
                response = await client.get("/api/service/catalog/sources?" + query)
                assert response.status_code in {400, 422} and "DO-NOT-ECHO" not in response.text
            assert (await client.get("/api/health?limit=1")).status_code == 400
            assert (await client.post(path + "/check?binding_id=x", json={})).status_code == 400
            assert (await client.delete("/api/session")).status_code == 204
            assert (await client.get(path + "/entries?limit=1")).status_code == 401


async def test_legacy_rows_are_explicit_and_never_acquire_new_provenance(pg_db):
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        source, body = await create_source(client)
        await services.catalog.record_discovery(UUID(body["provider_id"]), ["old-unverified"])
        response = await client.get("/api/service/catalog/sources")
        assert response.json()["legacy"] == [{"provider_id": body["provider_id"],
            "upstream_id": "old-unverified", "provenance": "legacy"}]
        assert (await client.get(f"/api/service/catalog/sources/{source['id']}/entries")).json()["items"] == []


async def test_snapshot_pagination_decisions_publish_exact_coefficients(pg_db):
    from tests.gateway.catalog_support import complete_snapshot, observe
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        source, _ = await create_source(client)
        identity = UUID(source["id"])
        run = await complete_snapshot(services.snapshots, services.sources, identity, (observe("a"), observe("b")))
        path = f"/api/service/catalog/sources/{identity}"
        first = await client.get(path + "/entries?limit=1")
        assert first.status_code == 200, first.text
        assert first.json()["items"][0]["upstream_id"] == "a"
        cursor = first.json()["next_cursor"]
        await complete_snapshot(services.snapshots, services.sources, identity, (observe("z"),))
        second = await client.get(path + "/entries", params={"cursor": cursor, "limit": 1})
        assert second.json()["run_id"] == str(run)
        assert second.json()["items"][0]["upstream_id"] == "b"
        assert (await client.get(path + "/entries?limit=201")).status_code == 422
        assert (await client.get(path + "/entries", params={"cursor": cursor + "x"})).status_code == 422
        ignored = await client.put(path + "/decisions", json={"version": 1, "decisions": [{"upstream_id": "z", "ignored": True}]})
        assert ignored.status_code == 200, ignored.text
        assert ignored.json()["decisions"][0]["version"] == 1
        assert (await client.put(path + "/decisions", json={"version": 1, "decisions": [{"upstream_id": "z", "ignored": False}]})).status_code == 409
        latest = (await client.get(path + "/entries")).json()
        expected = (await client.get("/api/service/catalog/sources")).json()["sources"][0]["expected"]
        command = {"operation_id": str(uuid4()), "run_id": latest["run_id"], "expected": expected,
            "selections": [{"upstream_id": "z", "public_model_id": "reviewed", "identity": "manual-family",
                "input_bound": 1000, "output_bound": 100, "capabilities": ["text"], "decision_version": 1,
                "new_model": {"model_id": "reviewed", "identity": "manual-family", "input_micro": "100000000000000123456"}}]}
        published = await client.post(path + "/publish", json=command)
        assert published.status_code == 200, published.text
        assert (await client.post(path + "/publish", json=command)).json() == published.json()
        model = (await client.get("/api/service/models")).json()[0]
        assert model["input_micro"] == "100000000000000123456" and model["enabled"] is False
        binding = (await client.get("/api/service/bindings")).json()[0]
        assert binding["enabled"] is False and binding["capabilities"] == ["text"]
        command["selections"][0]["input_bound"] = 999
        assert (await client.post(path + "/publish", json=command)).status_code == 409


async def test_probe_quote_ack_exact_cost_replay_and_reconcile(pg_db):
    from decimal import Decimal
    from autobuild_json.gateway.metering.costs import CostSchedule
    from autobuild_json.gateway.routing.records import BindingConfig, ModelConfig
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        source, _ = await create_source(client)
        identity = UUID(source["id"])
        context = await services.sources.context(identity)
        budget = await services.budgets.create("USD", Decimal("1000000000000000000000"))
        provider = context.provider.model_copy(update={"budget_id": budget,
            "cost_schedule": CostSchedule("USD", Decimal("1.000000000001"), Decimal("3"))})
        await services.catalog.update_provider(context.source.provider_id, 1, provider)
        await services.catalog.put_model(ModelConfig(model_id="p", identity="p"))
        binding = BindingConfig(uuid4(), context.source.provider_id, context.source.credential_id,
                                "p", "upstream", "p", 1000, 100, enabled=False)
        await services.catalog.put_binding(binding)
        path = f"/api/service/catalog/sources/{identity}"
        response = await client.get(path + "/probe-quote", params={"binding_id": str(binding.id)})
        assert response.status_code == 200, response.text
        quote = response.json()
        assert quote["upper_cost"] == "0.001048000001"
        assert quote["cost_schedule"]["input_per_million"] == "1.000000000001"
        intent = {"binding_id": str(binding.id), "quote_digest": quote["digest"], "expected": quote["stamp"],
                  "max_hold": quote["upper_cost"], "currency": "USD"}
        command = {"operation_id": str(uuid4()), "expected": quote["stamp"], "probe": intent}
        assert (await client.post(path + "/probe", json=command)).status_code == 422
        for bad in (False, 1, "true"):
            assert (await client.post(path + "/probe", json=dict(command, probe=dict(intent, acknowledged=bad)))).status_code == 422
        command["probe"]["acknowledged"] = True
        assert (await client.post(path + "/probe", json=dict(command, probe=dict(intent, max_hold=0.1)))).status_code == 422
        accepted = await client.post(path + "/probe", json=command)
        assert accepted.status_code == 202, accepted.text
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM upstream_reservations")) == 0
            assert await session.scalar(text("SELECT count(*) FROM requests")) == 0
        claim = await services.operations.claim(UUID(accepted.json()["id"]), uuid4())
        live = await services.quotes.quote(identity, binding.id)
        await services.probe_accounting.reserve(claim, live)
        await services.probe_accounting.mark_dispatched(claim)
        pending = await services.probe_accounting.settle(claim.operation_id, claim=claim)
        assert pending.state == "usage_pending"
        url = f"/api/service/provider-operations/{pending.id}/reconcile"
        amount = "100000000000000000001.000000000001"
        body = {"version": pending.version, "actual": amount, "currency": "USD", "reason": "Verified invoice"}
        reconciled = await client.post(url, json=body)
        assert reconciled.status_code == 200, reconciled.text
        assert reconciled.json()["cost"] == amount and reconciled.json()["usage"] is None
        assert (await client.post(url, json=body)).json() == reconciled.json()
        assert (await client.post(path + "/probe", json=command)).status_code == 200
        assert (await client.post(url, json=dict(body, actual="NaN"))).status_code == 422
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM upstream_reservations")) == 1
            assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='catalog.probe.reconciled' AND actor='admin'")) == 1


@pytest.mark.parametrize("cost", ["NaN", "Infinity", "-1", "0.0000000000001"])
async def test_malformed_persisted_cost_is_safe_state_error(pg_db, cost):
    import json
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        source, _ = await create_source(client)
        expected = (await client.get("/api/service/catalog/sources")).json()["sources"][0]["expected"]
        job = (await client.post(f"/api/service/catalog/sources/{source['id']}/check",
            json={"operation_id": str(uuid4()), "expected": expected})).json()
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE provider_operations SET cost=CAST(:cost AS jsonb) WHERE id=:id"),
                                  {"cost": json.dumps({"amount": cost}), "id": UUID(job["id"])})
        result = await client.get(f"/api/service/provider-operations/{job['id']}")
        assert result.status_code == 409 and result.json()["error"]["code"] == "invalid_state"


async def test_worker_availability_is_shared_live_database_heartbeat(pg_db):
    import asyncio
    from autobuild_json.gateway.admin.services import AdminServices
    from tests.gateway.catalog_support import synthetic_vault
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        await create_source(client)
        other = AdminServices(pg_db, synthetic_vault(), b"p" * 32)
        stopped = asyncio.Event()
        running = asyncio.create_task(other.catalog_worker.run(stopped))
        try:
            for _ in range(100):
                rows = (await client.get("/api/service/catalog/sources")).json()["sources"]
                if rows[0]["worker_available"]:
                    break
                await asyncio.sleep(.01)
            assert rows[0]["worker_available"] is True
            async with pg_db.sessions.begin() as session:
                await session.execute(text("UPDATE proxy_leases SET hard_deadline=clock_timestamp()-interval '1 second' WHERE resource LIKE 'catalog-worker:%'"))
            assert not await services.catalog_worker.available()
        finally:
            stopped.set()
            await running
        assert not (await client.get("/api/service/catalog/sources")).json()["sources"][0]["worker_available"]


async def test_safe_retry_after_is_only_validated_header(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        async def unavailable(*args):
            raise GatewayError("rate_limited", 429, "upstream", 7)
        services.operations.get = unavailable
        response = await client.get(f"/api/service/provider-operations/{uuid4()}")
        assert response.status_code == 429 and response.headers["retry-after"] == "7"
        assert response.headers["cache-control"] == "no-store"


async def test_all_catalog_routes_require_auth_and_mutation_csrf(pg_db):
    identity = str(uuid4())
    source = f"/api/service/catalog/sources/{identity}"
    operation = f"/api/service/provider-operations/{identity}"
    routes = [("GET", "/api/service/provider-presets"), ("GET", "/api/service/catalog/sources"),
        ("POST", "/api/service/catalog/sources"), ("PUT", source), ("GET", source + "/entries"),
        ("GET", source + "/probe-quote?binding_id=" + identity), ("GET", operation),
        *(('POST', source + '/' + kind) for kind in ("check", "discover", "probe", "publish")),
        ("PUT", source + "/decisions"), ("POST", operation + "/cancel"), ("POST", operation + "/reconcile")]
    async with admin_env(pg_db) as (client, services):
        for method, path in routes:
            assert (await client.request(method, path, json={} if method != "GET" else None)).status_code == 401
        client.headers["x-test-session"] = "synthetic-session"
        for method, path in routes:
            if method != "GET":
                assert (await client.request(method, path, json={})).status_code == 403
        client.headers.update(AUTH)
        for method, path in routes:
            if method != "GET":
                response = await client.request(method, path, json={"secret": "DO-NOT-ECHO", "actor": "root"})
                assert response.status_code == 422 and "DO-NOT-ECHO" not in response.text


async def test_disconnect_after_async_enqueue_cannot_cancel_durable_work(pg_db, monkeypatch):
    import asyncio
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        source, _ = await create_source(client)
        expected = (await client.get("/api/service/catalog/sources")).json()["sources"][0]["expected"]
        identity = uuid4()
        committed = asyncio.Event()
        enqueue = services.operations.enqueue
        async def gate(*args):
            result = await enqueue(*args)
            committed.set()
            await asyncio.Event().wait()
            return result
        monkeypatch.setattr(services.operations, "enqueue", gate)
        request = asyncio.create_task(client.post(f"/api/service/catalog/sources/{source['id']}/discover",
            json={"operation_id": str(identity), "expected": expected}))
        await asyncio.wait_for(committed.wait(), 2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        job = await services.operations.get(identity)
        assert job.state == "queued" and not job.cancel_requested


async def test_invalid_usage_never_becomes_zero_usage(pg_db):
    import json
    async with admin_env(pg_db) as (client, services):
        client.headers.update(AUTH)
        source, _ = await create_source(client)
        expected = (await client.get("/api/service/catalog/sources")).json()["sources"][0]["expected"]
        identity = uuid4()
        await client.post(f"/api/service/catalog/sources/{source['id']}/check", json={"operation_id": str(identity), "expected": expected})
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE provider_operations SET usage=CAST(:value AS jsonb) WHERE id=:id"),
                                  {"value": json.dumps({"input_tokens": "bad", "output_tokens": 0}), "id": identity})
        response = await client.get(f"/api/service/provider-operations/{identity}")
        assert response.status_code == 409 and response.json()["error"]["code"] == "invalid_usage"
