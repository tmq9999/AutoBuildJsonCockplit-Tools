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


async def short_claim(case, db):
    claim = await case.ops.claim(case.job.id, uuid4())
    deadline = datetime.now(timezone.utc) + timedelta(seconds=.6)
    async with db.sessions.begin() as session:
        await session.execute(text("UPDATE provider_operations SET deadline=:deadline WHERE id=:id"),
                              {"deadline": deadline, "id": case.job.id})
    return claim.model_copy(update={"deadline": deadline})


@pytest.mark.parametrize("boundary", ["context", "profile", "proxy", "snapshot", "finish", "heartbeat"])
async def test_whole_operation_bounds_blocked_boundary(pg_db, monkeypatch, boundary):
    from contextlib import asynccontextmanager
    async with runner_case(pg_db, fixed=True, kind="check" if boundary == "finish" else "discover") as case:
        claim = await short_claim(case, pg_db)
        entered, closed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            try:
                await release.wait()
            finally:
                closed.set()
        if boundary == "context":
            monkeypatch.setattr(case.sources, "context", blocked)
        elif boundary == "profile":
            monkeypatch.setattr(case.profiles, "load", blocked)
        elif boundary == "proxy":
            @asynccontextmanager
            async def acquire(*args, **kwargs):
                await blocked()
                yield
            monkeypatch.setattr(case.runner.proxies, "acquire", acquire)
        elif boundary == "snapshot":
            monkeypatch.setattr(case.snapshots, "commit", blocked)
        elif boundary == "finish":
            monkeypatch.setattr(case.ops, "finish", blocked)
        else:
            # Block the heartbeat's database read while HTTP is active.
            claim = claim.model_copy(update={"deadline": datetime.now(timezone.utc) + timedelta(seconds=1.2)})
            async with pg_db.sessions.begin() as session:
                await session.execute(text("UPDATE provider_operations SET deadline=:deadline WHERE id=:id"),
                                      {"deadline": claim.deadline, "id": case.job.id})
            monkeypatch.setattr(case.ops, "heartbeat", blocked)
            case.pause_after_first_page.set()
        task = asyncio.create_task(case.runner.execute(claim))
        await asyncio.wait_for(entered.wait(), 2)
        done, _ = await asyncio.wait({task}, timeout=1)
        if not done:
            release.set()
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert done, f"{boundary} outlived the operation deadline"
        assert closed.is_set()
        assert (await case.ops.get(case.job.id)).state == "running"
        await assert_released(pg_db)


async def test_source_disable_heartbeat_aborts_blocked_http(pg_db):
    async with runner_case(pg_db, fixed=True) as case:
        case.pause_after_first_page.set()
        task = asyncio.create_task(case.runner.run(case.job.id))
        await asyncio.wait_for(case.first_page_entered.wait(), 5)
        await case.sources.update(case.source.id, case.source.version,
                                  case.source_input.model_copy(update={"enabled": False}), "admin")
        done, _ = await asyncio.wait({task}, timeout=2)
        if not done:
            task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        assert done, "source invalidation did not interrupt HTTP"
        assert results[0].state == "stale"
        assert case.http_closed.is_set()
        await assert_released(pg_db)


async def test_cancel_between_heartbeat_check_and_update_finishes_cancelled(pg_db, monkeypatch):
    """A cancel arriving after check_current must not be mistaken for fence loss."""
    async with runner_case(pg_db, fixed=True) as case:
        case.pause_after_first_page.set()
        entered, release = asyncio.Event(), asyncio.Event()
        original_heartbeat = case.ops.heartbeat

        async def gated_heartbeat(claim):
            entered.set()
            await release.wait()
            return await original_heartbeat(claim)

        monkeypatch.setattr(case.ops, "heartbeat", gated_heartbeat)
        task = asyncio.create_task(case.runner.run(case.job.id))
        await asyncio.wait_for(entered.wait(), 5)
        current = await case.ops.get(case.job.id)
        await case.ops.cancel(current.id, current.version, "admin")
        release.set()
        result = await asyncio.wait_for(task, 5)
        assert result.state == "cancelled"
        assert result.error == "operation_cancelled"
        await assert_released(pg_db)


async def test_check_final_write_rechecks_source_under_lock(pg_db, monkeypatch):
    async with runner_case(pg_db, kind="check") as case:
        finish = case.ops.finish
        async def change_before_write(claim, state, **kwargs):
            if state == "succeeded":
                await case.catalog.rotate_credential(case.source.credential_id, "synthetic-new")
            return await finish(claim, state, **kwargs)
        monkeypatch.setattr(case.ops, "finish", change_before_write)
        result = await case.runner.run(case.job.id)
        assert result.state == "stale"
        assert result.result.get("catalog_readable") is not True


@pytest.mark.parametrize("failure", [RuntimeError("synthetic write failure"), GatewayError("storage_unavailable", 503)])
async def test_snapshot_write_failure_stays_recoverable(pg_db, monkeypatch, failure):
    async with runner_case(pg_db) as case:
        async def fail(*args, **kwargs):
            raise failure
        monkeypatch.setattr(case.snapshots, "commit", fail)
        with pytest.raises(GatewayError, match="storage_unavailable"):
            await case.runner.run(case.job.id)
        assert (await case.ops.get(case.job.id)).state == "running"
        assert (await case.sources.get(case.source.id)).latest_successful_run_id is None
        await assert_released(pg_db)


async def test_expired_cleanup_cannot_finalize_without_recovery(pg_db):
    async with runner_case(pg_db) as case:
        claim = await case.ops.claim(case.job.id, uuid4())
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE provider_operations SET deadline=clock_timestamp()-interval '1 second' WHERE id=:id"),
                                  {"id": case.job.id})
        with pytest.raises(GatewayError, match="claim_lost"):
            await case.ops.finish_fenced(claim, "cancelled", error="operation_cancelled")
        assert (await case.ops.get(case.job.id)).state == "running"


@pytest.mark.parametrize("retry_after", [None, "invalid"])
async def test_429_without_valid_retry_after_defaults_to_sixty(pg_db, retry_after):
    def upstream(request):
        return httpx.Response(429, headers={} if retry_after is None else {"Retry-After": retry_after})
    async with runner_case(pg_db, upstream=upstream) as case:
        result = await case.runner.run(case.job.id)
        assert result.error == "rate_limited"
        async with pg_db.sessions() as session:
            remaining = await session.scalar(text("SELECT extract(epoch FROM cooldown_until-clock_timestamp()) FROM providers WHERE id=:id"),
                                             {"id": case.source.provider_id})
        assert remaining is not None and 55 <= remaining <= 60


async def test_429_cooldown_write_failure_stays_recoverable(pg_db, monkeypatch):
    async with runner_case(pg_db, upstream=lambda request: httpx.Response(429, headers={"Retry-After": "30"})) as case:
        async def fail(*args, **kwargs):
            raise RuntimeError("synthetic database failure")
        monkeypatch.setattr(case.catalog, "cooldown", fail)
        with pytest.raises(GatewayError, match="storage_unavailable"):
            await case.runner.run(case.job.id)
        assert (await case.ops.get(case.job.id)).state == "running"


async def test_429_uses_response_receipt_not_operation_start(pg_db, monkeypatch):
    import time
    receipt = []
    def upstream(request):
        receipt.append(time.monotonic())
        return httpx.Response(429, headers={"Retry-After": "30"})
    async with runner_case(pg_db, upstream=upstream) as case:
        cooldown = case.catalog.cooldown
        persisted = []
        async def record(*args, **kwargs):
            persisted.append(kwargs["started_at"])
            return await cooldown(*args, **kwargs)
        monkeypatch.setattr(case.catalog, "cooldown", record)
        await case.runner.run(case.job.id)
        assert len(persisted) == 1
        assert persisted[0] >= receipt[0]


async def test_snapshot_transaction_failure_rolls_back_entries_warnings_and_success(pg_db):
    def upstream(request):
        return httpx.Response(200, json={"models": [{"name": "models/a", "inputTokenLimit": "bad"}]})
    async with runner_case(pg_db, mode="gemini", upstream=upstream) as case:
        async with pg_db.sessions.begin() as session:
            await session.execute(text("""CREATE FUNCTION reject_snapshot_success() RETURNS trigger AS $$
                BEGIN IF NEW.state='succeeded' THEN RAISE EXCEPTION 'synthetic failure'; END IF;
                RETURN NEW; END; $$ LANGUAGE plpgsql"""))
            await session.execute(text("""CREATE TRIGGER reject_snapshot_success BEFORE UPDATE ON provider_operations
                FOR EACH ROW EXECUTE FUNCTION reject_snapshot_success()"""))
        with pytest.raises(GatewayError, match="storage_unavailable"):
            await case.runner.run(case.job.id)
        stored = await case.ops.get(case.job.id)
        assert stored.state == "running"
        assert stored.result == {}
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM catalog_entries WHERE run_id=:id"),
                                        {"id": case.job.id}) == 0
        assert (await case.sources.get(case.source.id)).latest_successful_run_id is None


async def test_check_terminal_write_failure_is_not_rewritten_failed(pg_db, monkeypatch):
    async with runner_case(pg_db, kind="check") as case:
        finish = case.ops.finish
        async def fail_success(claim, state, **kwargs):
            if state == "succeeded":
                raise RuntimeError("synthetic terminal write failure")
            return await finish(claim, state, **kwargs)
        monkeypatch.setattr(case.ops, "finish", fail_success)
        with pytest.raises(GatewayError, match="storage_unavailable"):
            await case.runner.run(case.job.id)
        assert (await case.ops.get(case.job.id)).state == "running"


async def test_repeated_caller_cancel_joins_http_and_proxy_cleanup(pg_db, monkeypatch):
    async with runner_case(pg_db, fixed=True) as case:
        case.pause_after_first_page.set()
        release = case.runner.proxies.store.release
        cleaning, proceed = asyncio.Event(), asyncio.Event()
        async def gated_release(token):
            cleaning.set()
            await proceed.wait()
            await release(token)
        monkeypatch.setattr(case.runner.proxies.store, "release", gated_release)
        before = asyncio.all_tasks()
        task = asyncio.create_task(case.runner.run(case.job.id))
        await asyncio.wait_for(case.first_page_entered.wait(), 5)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 5)
        task.cancel()
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert case.http_closed.is_set()
        await assert_released(pg_db)
        assert (await case.ops.get(case.job.id)).state == "cancelled"
        assert not (asyncio.all_tasks() - before)


async def test_page_timeout_cleanup_survives_simultaneous_outer_cancellation(pg_db, monkeypatch):
    """A real PG admission release must complete when both deadline layers fire."""
    from contextlib import asynccontextmanager
    import time
    async with runner_case(pg_db, fixed=True) as case:
        acquire = case.runner.discovery.limits.acquire
        cleaning, proceed = asyncio.Event(), asyncio.Event()
        @asynccontextmanager
        async def gated_admission(route, deadline):
            async with acquire(route, deadline) as admission:
                try:
                    yield admission
                finally:
                    cleaning.set()
                    await proceed.wait()
        monkeypatch.setattr(case.runner.discovery.limits, "acquire", gated_admission)
        collect = case.runner.discovery.collect
        async def short_page(context, lease, check_current, deadline, **kwargs):
            return await collect(context, lease, check_current, time.monotonic()+.15, **kwargs)
        monkeypatch.setattr(case.runner.discovery, "collect", short_page)
        case.pause_after_first_page.set()
        before = asyncio.all_tasks()
        task = asyncio.create_task(case.runner.run(case.job.id))
        await asyncio.wait_for(case.first_page_entered.wait(), 5)
        await asyncio.wait_for(cleaning.wait(), 5)
        task.cancel()
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await assert_released(pg_db)
        assert not (asyncio.all_tasks() - before)


async def test_cleanup_finalization_budget_failure_is_safe_and_joined(pg_db, monkeypatch):
    async with runner_case(pg_db) as case:
        claim = await short_claim(case, pg_db)
        original_wait_for, get = asyncio.wait_for, case.ops.get
        entered, closed = asyncio.Event(), asyncio.Event()
        async def blocked_get(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        async def short_cleanup(awaitable, timeout):
            return await original_wait_for(awaitable, .02 if timeout == 5 else timeout)
        monkeypatch.setattr(case.ops, "get", blocked_get)
        monkeypatch.setattr(asyncio, "wait_for", short_cleanup)
        case.pause_after_first_page.set()
        before = asyncio.all_tasks()
        with pytest.raises(GatewayError, match="storage_unavailable"):
            await case.runner.execute(claim)
        assert entered.is_set() and closed.is_set()
        assert (await get(case.job.id)).state == "running"
        await assert_released(pg_db)
        assert not (asyncio.all_tasks() - before)
