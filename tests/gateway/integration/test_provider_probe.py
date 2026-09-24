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
