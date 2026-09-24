import json
import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
import httpx
from sqlalchemy import text

from tests.gateway.catalog_support import probe_case
from tests.gateway.harness import chat_success

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_probe_replay_charges_upstream_once_not_the_customer(pg_db):
    async with probe_case(pg_db) as case:
        first = await case.ops.enqueue(case.command, "admin")
        alias = case.command.model_copy(update={"id": uuid4()})
        assert (await case.ops.enqueue(alias, "admin")).id == first.id
        result = await case.runner.run(first.id)
        for command in (case.command, alias):
            replay = await case.ops.enqueue(command, "admin")
            await case.runner.run(replay.id)
        assert case.generation_calls == 1
        assert result.state == "succeeded" and result.result["inference_verified"] is True
        assert result.cost == Decimal("0.000019")
        body = json.loads(case.requests[0].content)
        assert body["messages"] == [{"role": "user", "content": "Reply with OK."}]
        assert body["max_completion_tokens"] == 16
        assert "tools" not in body and "previous_response_id" not in body
        async with pg_db.sessions() as session:
            for table in ("requests", "usage_ledger", "attempts", "quota_buckets"):
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
            assert await session.scalar(text("SELECT actual FROM upstream_reservations")) == Decimal("0.000019")
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions")) == 1
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0


async def test_probe_429_records_rejection_before_response_cleanup_and_settles_zero(pg_db):
    async def upstream(request):
        return httpx.Response(429, headers={"retry-after": "30"})

    async with probe_case(pg_db, upstream=upstream) as case:
        operation = await case.ops.enqueue(case.command, "admin")
        result = await case.runner.run(operation.id)
        assert result.state == "failed" and result.cost == 0
        assert result.result["rejected_before_generation"] is True
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT retry_after FROM provider_operations WHERE id=:id"),
                                        {"id": operation.id}) == 30
            assert await session.scalar(text("SELECT held FROM upstream_budgets WHERE id=:id"),
                                        {"id": case.budget}) == 0
            assert await session.scalar(text("SELECT cooldown_until > clock_timestamp() FROM providers WHERE id=:id"),
                                        {"id": case.source.provider_id}) is True


@pytest.mark.parametrize("mode", ["direct", "fixed", "pool"])
@pytest.mark.parametrize("outcome,state,cost", [
    ("success", "succeeded", Decimal("0.000019")), ("zero", "succeeded", Decimal(0)),
    ("missing", "usage_pending", None), ("truncated", "usage_pending", None),
    ("cache", "usage_pending", None), ("tool", "usage_pending", None),
    ("500", "usage_pending", None), ("401", "failed", Decimal(0)),
])
async def test_single_attempt_outcomes_release_slots_preserve_money(pg_db, mode, outcome, state, cost):
    def upstream(request):
        if outcome in {"500", "401"}:
            return httpx.Response(int(outcome))
        payload = chat_success(outcome != "missing")
        if outcome == "truncated":
            payload = payload.removesuffix(b"data: [DONE]\n\n")
        elif outcome == "cache":
            payload = payload.replace(b'"prompt_tokens": 4', b'"prompt_tokens_details": {"cached_tokens": 2}, "prompt_tokens": 4')
        elif outcome == "tool":
            payload = b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call","function":{"name":"never","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, content=payload)

    async with probe_case(pg_db, upstream=upstream, zero=outcome == "zero", proxy_mode=mode) as case:
        operation = await case.ops.enqueue(case.command, "admin")
        result = await case.runner.run(operation.id)
        assert (result.state, result.cost) == (state, cost)
        assert case.generation_calls == 1
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions")) == 1
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0
            assert await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL")) == 0
            assert await session.scalar(text("SELECT held FROM upstream_budgets")) == (
                Decimal("0.001048") if state == "usage_pending" else 0)
            row = (await session.execute(text("SELECT input,result,usage FROM provider_operations WHERE id=:id"),
                                         {"id": operation.id})).one()
            assert "Hello" not in str(row) and "Reply with OK." not in str(row)


async def test_definite_rejection_is_durable_before_slow_http_cleanup(pg_db):
    closing, release = asyncio.Event(), asyncio.Event()
    class ResponseBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"private response"
        async def aclose(self):
            closing.set()
            await release.wait()

    async with probe_case(pg_db, upstream=lambda request: httpx.Response(429, headers={"retry-after": "2"}, stream=ResponseBody())) as case:
        operation = await case.ops.enqueue(case.command, "admin")
        task = asyncio.create_task(case.runner.run(operation.id))
        try:
            await asyncio.wait_for(closing.wait(), 3)
            view = await case.ops.get(operation.id)
            assert view.result["rejected_before_generation"] is True
            assert view.state == "running" and view.cost is None
            await asyncio.sleep(0.1)
        finally:
            release.set()
            result = await task
        assert result.cost == 0 and case.generation_calls == 1


async def test_local_preflight_failure_has_safe_error_and_no_dispatch(pg_db):
    async with probe_case(pg_db) as case:
        operation = await case.ops.enqueue(case.command, "admin")
        case.services.transport.policy.resolver = None
        # Invalid origin is rejected by live egress without any HTTP dispatch.
        async def blocked(host, port):
            return ["127.0.0.1"]
        case.services.transport.policy.resolver = blocked
        view = await case.runner.run(operation.id)
        assert view.state == "failed" and view.error == "egress_denied"
        assert view.dispatched_at is None and case.generation_calls == 0


async def test_output_bound_violation_records_usage_and_quarantines_binding(pg_db):
    async with probe_case(pg_db, upstream=lambda request: httpx.Response(200,
            content=chat_success().replace(b'"completion_tokens": 5', b'"completion_tokens": 17'))) as case:
        operation = await case.ops.enqueue(case.command, "admin")
        view = await case.runner.run(operation.id)
        assert view.usage.output_tokens == 17 and view.cost == Decimal("0.000055")
        assert view.result["inference_verified"] is False
        async with pg_db.sessions() as session:
            binding = (await session.execute(text("SELECT version,config FROM model_bindings WHERE id=:id"),
                                             {"id": case.binding.id})).one()
            assert binding.config["enabled"] is False and binding.version == 2


async def test_probe_pins_preflight_credential_without_second_resolution(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    async with probe_case(pg_db) as case:
        adapter = case.services.engine.adapters["openai_compatible"]
        resolve = adapter.credential_resolver
        calls = 0
        async def once(route):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise GatewayError("secret_unavailable")
            return await resolve(route)
        adapter.credential_resolver = once
        operation = await case.ops.enqueue(case.command, "admin")
        view = await case.runner.run(operation.id)
        assert view.state == "succeeded" and case.generation_calls == 1
        assert calls == 1
        assert case.requests[0].headers["authorization"] == "Bearer synthetic-probe-key"


async def test_unknown_cache_cost_still_quarantines_overbound_usage(pg_db):
    payload = chat_success().replace(b'"completion_tokens": 5', b'"completion_tokens": 17').replace(
        b'"prompt_tokens": 4', b'"prompt_tokens": 4, "prompt_tokens_details": {"cached_tokens": 2}')
    async with probe_case(pg_db, upstream=lambda request: httpx.Response(200, content=payload)) as case:
        operation = await case.ops.enqueue(case.command, "admin")
        view = await case.runner.run(operation.id)
        assert view.state == "usage_pending" and view.cost is None and view.usage.output_tokens == 17
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT config->'enabled' FROM model_bindings WHERE id=:id"),
                                        {"id": case.binding.id}) is False
            assert await session.scalar(text("SELECT held FROM upstream_budgets")) == Decimal("0.001048")


async def test_probe_kind_lookup_is_deadline_bounded_and_joined(pg_db, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from autobuild_json.gateway.errors import GatewayError
    async with probe_case(pg_db) as case:
        operation = await case.ops.enqueue(case.command, "admin")
        claim = await case.ops.claim(operation.id, uuid4())
        claim = claim.model_copy(update={"deadline": datetime.now(timezone.utc) + timedelta(seconds=.05)})
        closed = asyncio.Event()
        async def blocked_get(identity):
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        monkeypatch.setattr(case.ops, "get", blocked_get)
        before = asyncio.all_tasks()
        with pytest.raises(GatewayError, match="storage_unavailable"):
            await asyncio.wait_for(case.runner.execute(claim), .5)
        assert closed.is_set() and not (asyncio.all_tasks() - before)


@pytest.mark.parametrize("seam,want", [("reserve", "cancelled"), ("dispatch", "usage_pending"),
                                       ("chunk", "usage_pending"), ("evidence", "succeeded")])
async def test_cancellation_at_durable_seams_releases_slots_and_never_replays(pg_db, monkeypatch, seam, want):
    entered, release = asyncio.Event(), asyncio.Event()
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
            entered.set()
            await release.wait()
        async def aclose(self):
            pass
    upstream = (lambda request: httpx.Response(200, stream=Body())) if seam == "chunk" else None
    async with probe_case(pg_db, proxy_mode="fixed", upstream=upstream) as case:
        accounting = case.runner.probe.accounting
        if seam != "chunk":
            method = {"reserve": "reserve", "dispatch": "mark_dispatched", "evidence": "record_evidence"}[seam]
            original = getattr(accounting, method)
            async def gate(*args, **kwargs):
                result = await original(*args, **kwargs)
                entered.set()
                await release.wait()
                return result
            monkeypatch.setattr(accounting, method, gate)
        operation = await case.ops.enqueue(case.command, "admin")
        before = asyncio.all_tasks()
        task = asyncio.create_task(case.runner.run(operation.id))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        view = await case.ops.get(operation.id)
        assert view.state == want
        await case.runner.run(operation.id)
        assert case.generation_calls == (1 if seam in {"chunk", "evidence"} else 0)
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0
            assert await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL")) == 0
            assert await session.scalar(text("SELECT held FROM upstream_budgets")) == (
                Decimal("0.001048") if want == "usage_pending" else 0)
        assert not (asyncio.all_tasks() - before)


async def test_kiot_probe_429_keeps_one_endpoint_and_no_rotation(pg_db):
    from autobuild_json.gateway.proxy.kiot import KiotClient
    from tests.gateway.unit.test_kiot import success
    paths = []
    def control(request):
        paths.append(request.url.path)
        return httpx.Response(200, json=success())
    async with probe_case(pg_db, proxy_mode="kiotproxy", upstream=lambda request: httpx.Response(429)) as case:
        case.services.proxies.kiot = KiotClient(adapter=httpx.MockTransport(control))
        operation = await case.ops.enqueue(case.command, "admin")
        result = await case.runner.run(operation.id)
        assert result.state == "failed" and result.cost == 0
        assert case.generation_calls == 1 and paths == ["/api/v1/proxies/current"]
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL")) == 0
            assert await session.scalar(text("SELECT count(*) FROM proxy_leases")) == 2


async def test_changed_binding_is_not_overwritten_by_overbound_probe(pg_db):
    entered, release = asyncio.Event(), asyncio.Event()
    async def upstream(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, content=chat_success().replace(b'"completion_tokens": 5', b'"completion_tokens": 17'))
    async with probe_case(pg_db, upstream=upstream) as case:
        operation = await case.ops.enqueue(case.command, "admin")
        task = asyncio.create_task(case.runner.run(operation.id))
        await asyncio.wait_for(entered.wait(), 3)
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE model_bindings SET version=version+1 WHERE id=:id"), {"id": case.binding.id})
        release.set()
        await task
        async with pg_db.sessions() as session:
            row = (await session.execute(text("SELECT version,config FROM model_bindings WHERE id=:id"), {"id": case.binding.id})).one()
            assert row.version == 2 and row.config["enabled"] is True
            details = await session.scalar(text("SELECT details FROM audit_events WHERE record_id=:id AND action='catalog.probe.binding_version_changed'"), {"id": operation.id})
            assert details["binding_version"] == 1 and details["current_version"] == 2


async def test_timeout_after_dispatch_retains_hold_for_fenced_recovery(pg_db):
    from datetime import datetime, timedelta, timezone
    entered, closed = asyncio.Event(), asyncio.Event()
    async def upstream(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    async with probe_case(pg_db, upstream=upstream, proxy_mode="fixed") as case:
        operation = await case.ops.enqueue(case.command, "admin")
        claim = await case.ops.claim(operation.id, uuid4())
        deadline = datetime.now(timezone.utc) + timedelta(seconds=.5)
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE provider_operations SET deadline=:deadline WHERE id=:id"), {"id": operation.id, "deadline": deadline})
        claim = claim.model_copy(update={"deadline": deadline})
        before = asyncio.all_tasks()
        view = await asyncio.wait_for(case.runner.execute(claim), 3)
        assert entered.is_set() and closed.is_set() and view.state == "running"
        async with pg_db.sessions.begin() as session:
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0
            assert await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL")) == 0
            await session.execute(text("UPDATE provider_operations SET deadline=clock_timestamp()-interval '16 seconds' WHERE id=:id"), {"id": operation.id})
        result = await case.runner.probe.recover(operation.id)
        assert result.state == "usage_pending" and result.cost is None
        assert not (asyncio.all_tasks() - before)


async def test_shared_resource_cleanup_is_once_and_joins_cancelled_waiter(pg_db):
    from contextlib import asynccontextmanager
    from autobuild_json.gateway.catalog.probe import _Resources
    entered, release = asyncio.Event(), asyncio.Event()
    exits = 0
    @asynccontextmanager
    async def resource():
        nonlocal exits
        try:
            yield
        finally:
            exits += 1
            entered.set()
            await release.wait()
    resources = _Resources()
    await resources.stack.enter_async_context(resource())
    before = asyncio.all_tasks()
    first = asyncio.create_task(resources.close())
    await entered.wait()
    second = asyncio.create_task(resources.close())
    first.cancel()
    await asyncio.sleep(0)
    assert not first.done() and not second.done()
    release.set()
    await asyncio.gather(first, second)
    assert exits == 1 and not (asyncio.all_tasks() - before)
