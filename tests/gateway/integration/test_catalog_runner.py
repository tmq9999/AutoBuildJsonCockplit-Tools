import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from autobuild_json.gateway.errors import GatewayError
from tests.gateway.catalog_support import runner_case

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def assert_released(db):
    async with db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0
        assert await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL")) == 0


@pytest.mark.parametrize("change", ["disable", "rotate", "root", "profile", "version"])
async def test_config_change_after_first_page_prevents_next_dispatch(pg_db, change):
    async with runner_case(pg_db, fixed=True) as case:
        case.pause_after_first_page.set()
        task = asyncio.create_task(case.runner.run(case.job.id))
        await asyncio.wait_for(case.first_page_entered.wait(), 5)
        if change == "disable":
            await case.sources.update(case.source.id, case.source.version,
                case.source_input.model_copy(update={"enabled": False}), "admin")
        elif change == "rotate":
            await case.catalog.rotate_credential(case.source.credential_id, "synthetic-rotated")
        elif change == "root":
            await case.catalog.update_provider(case.source.provider_id, case.context.stamp.provider_version,
                case.context.provider.model_copy(update={"root": "https://changed.invalid/v1"}))
        else:
            table, identity = (("proxy_profiles", case.context.effective_proxy_id) if change == "profile"
                               else ("providers", case.source.provider_id))
            async with pg_db.sessions.begin() as session:
                await session.execute(text(f"UPDATE {table} SET version=version+1 WHERE id=:id"), {"id": identity})
        case.release_first_page.set()
        result = await asyncio.wait_for(task, 5)
        assert result.state == "stale"
        assert case.sent_pages == 1
        assert (await case.sources.get(case.source.id)).latest_successful_run_id is None
        await assert_released(pg_db)


async def test_running_cancel_stops_http_and_finishes_after_cleanup(pg_db):
    async with runner_case(pg_db, fixed=True) as case:
        case.pause_after_first_page.set()
        task = asyncio.create_task(case.runner.run(case.job.id))
        await asyncio.wait_for(case.first_page_entered.wait(), 5)
        current = await case.ops.get(case.job.id)
        await case.ops.cancel(current.id, current.version, "admin")
        result = await asyncio.wait_for(task, 5)
        assert case.http_closed.is_set()
        assert result.state == "cancelled"
        assert result.error == "operation_cancelled"
        assert case.sent_pages == 1
        await assert_released(pg_db)


async def test_complete_snapshot_includes_metadata_and_local_warnings(pg_db):
    def upstream(request):
        return httpx.Response(200, json={"models": [{"name": "models/native", "inputTokenLimit": 4096,
            "outputTokenLimit": "bad", "displayName": "Native"}]})
    async with runner_case(pg_db, mode="gemini", upstream=upstream) as case:
        result = await case.runner.run(case.job.id)
        assert result.state == "succeeded"
        assert result.result["warnings"] == ["malformed_optional_metadata"]
        page = await case.snapshots.page(case.source.id)
        assert page.run_id == case.job.id
        assert page.items[0].entry.metadata["input_token_limit"].value == 4096
        assert (await case.runner.run(case.job.id)).id == result.id
        assert case.sent_pages == 1
        await assert_released(pg_db)


async def test_check_reads_one_page_without_snapshot(pg_db):
    async with runner_case(pg_db, kind="check") as case:
        result = await case.runner.run(case.job.id)
        assert result.state == "succeeded"
        assert result.result["catalog_readable"] is True
        assert result.result["authentication"] == "unverified"
        assert case.sent_pages == 1
        assert (await case.sources.get(case.source.id)).latest_successful_run_id is None


async def test_queued_cancel_never_dispatches(pg_db):
    async with runner_case(pg_db) as case:
        await case.ops.cancel(case.job.id, case.job.version, "admin")
        assert (await case.runner.run(case.job.id)).state == "cancelled"
        assert case.sent_pages == 0


async def test_simultaneous_runs_claim_only_once(pg_db):
    async with runner_case(pg_db) as case:
        results = await asyncio.gather(case.runner.run(case.job.id), case.runner.run(case.job.id))
        assert any(result.state == "succeeded" for result in results)
        assert case.sent_pages == 2


async def test_context_changed_between_claim_and_execute_never_dispatches(pg_db):
    async with runner_case(pg_db) as case:
        claim = await case.ops.claim(case.job.id, uuid4())
        await case.catalog.rotate_credential(case.source.credential_id, "synthetic-rotated")
        result = await case.runner.execute(claim)
        assert result.state == "stale"
        assert case.sent_pages == 0


async def test_operation_fence_loss_stops_active_http(pg_db):
    async with runner_case(pg_db, fixed=True) as case:
        case.pause_after_first_page.set()
        task = asyncio.create_task(case.runner.run(case.job.id))
        await asyncio.wait_for(case.first_page_entered.wait(), 5)
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE provider_operations SET generation=generation+1 WHERE id=:id"),
                                  {"id": case.job.id})
        result = await asyncio.wait_for(task, 5)
        assert case.http_closed.is_set()
        assert result.state == "running"
        assert (await case.sources.get(case.source.id)).latest_successful_run_id is None
        await assert_released(pg_db)


async def test_deadline_aborts_http_leaving_fenced_recovery_work(pg_db):
    async with runner_case(pg_db, fixed=True) as case:
        claim = await case.ops.claim(case.job.id, uuid4())
        deadline = datetime.now(timezone.utc) + timedelta(seconds=.25)
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE provider_operations SET deadline=:deadline WHERE id=:id"),
                                  {"id": case.job.id, "deadline": deadline})
        claim = claim.model_copy(update={"deadline": deadline})
        case.pause_after_first_page.set()
        result = await asyncio.wait_for(case.runner.execute(claim), 5)
        assert result.state == "running"
        assert case.http_closed.is_set()
        assert (await case.sources.get(case.source.id)).latest_successful_run_id is None
        await assert_released(pg_db)


async def test_later_429_keeps_prior_snapshot_and_persists_cooldown(pg_db):
    def upstream(request):
        if request.url.params.get("after"):
            return httpx.Response(429, headers={"Retry-After": "30"})
        return httpx.Response(200, json={"data": [{"id": "first"}], "has_more": True, "last_id": "first"})
    async with runner_case(pg_db, upstream=upstream) as case:
        result = await case.runner.run(case.job.id)
        assert result.state == "failed"
        assert result.error == "rate_limited"
        assert (await case.sources.get(case.source.id)).latest_successful_run_id is None
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT cooldown_until>clock_timestamp() FROM providers WHERE id=:id"),
                                        {"id": case.source.provider_id})
            assert await session.scalar(text("SELECT health FROM credentials WHERE id=:id"),
                                        {"id": case.source.credential_id}) == "active"
        await assert_released(pg_db)


async def test_running_cancel_allows_only_cancelled_terminal_write(pg_db):
    from tests.gateway.catalog_support import source_case
    from autobuild_json.gateway.catalog.operations import OperationStore
    from autobuild_json.gateway.catalog.records import OperationRequest
    sources, source = await source_case(pg_db)
    ops = OperationStore(pg_db, sources)
    job = await ops.enqueue(OperationRequest(id=uuid4(), source_id=source.id, kind="check",
                                           expected=(await sources.context(source.id)).stamp), "admin")
    claim = await ops.claim(job.id, uuid4())
    running = await ops.get(job.id)
    await ops.cancel(job.id, running.version, "admin")
    with pytest.raises(GatewayError, match="claim_lost"):
        await ops.finish(claim, "succeeded", result={"reachable": True})
    assert (await ops.finish_fenced(claim, "cancelled", error="operation_cancelled")).state == "cancelled"
