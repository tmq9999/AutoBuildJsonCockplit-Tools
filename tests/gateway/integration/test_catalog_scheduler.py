import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text

from autobuild_json.gateway.catalog.operations import OperationStore
from autobuild_json.gateway.catalog.records import OperationRequest
from tests.gateway.catalog_support import source_case

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def due(db, source):
    async with db.sessions.begin() as session:
        await session.execute(text("UPDATE catalog_sources SET schedule_enabled=true,interval_seconds=300,"
                                   "next_run_at=clock_timestamp()-interval '7 days' WHERE id=:id"), {"id": source.id})


async def test_two_schedulers_claim_one_due_job(pg_db):
    from autobuild_json.gateway.catalog.scheduler import Scheduler
    sources, source = await source_case(pg_db)
    await due(pg_db, source)
    ops = OperationStore(pg_db, sources)
    a, b = Scheduler(pg_db, ops, sources), Scheduler(pg_db, ops, sources)
    results = await asyncio.gather(a.tick(), b.tick())
    assert sum(map(len, results)) == 1
    assert await a.tick() == []
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM provider_operations WHERE state='queued'")) == 1
        assert await session.scalar(text("SELECT next_run_at>clock_timestamp() FROM catalog_sources WHERE id=:id"),
                                    {"id": source.id})


async def test_default_off_busy_disabled_and_cooldown_skip(pg_db):
    from autobuild_json.gateway.catalog.scheduler import Scheduler
    sources, source = await source_case(pg_db)
    ops = OperationStore(pg_db, sources)
    scheduler = Scheduler(pg_db, ops, sources)
    assert await scheduler.tick() == []
    await due(pg_db, source)
    context = await sources.context(source.id)
    job = await ops.enqueue(OperationRequest(id=uuid4(), source_id=source.id, kind="check", expected=context.stamp), "test")
    assert await scheduler.tick() == []
    await ops.cancel(job.id, job.version, "test")
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE providers SET cooldown_until=clock_timestamp()+interval '2 hours' WHERE id=:id"),
                              {"id": source.provider_id})
    assert await scheduler.tick() == []
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE providers SET cooldown_until=NULL,config=jsonb_set(config,'{enabled}','false') WHERE id=:id"),
                              {"id": source.provider_id})
    assert await scheduler.tick() == []


async def test_failed_completion_uses_db_backoff_cooldown_and_is_idempotent(pg_db):
    from autobuild_json.gateway.catalog.scheduler import Scheduler
    sources, source = await source_case(pg_db)
    await due(pg_db, source)
    scheduler = Scheduler(pg_db, OperationStore(pg_db, sources), sources, jitter=lambda a, b: 17)
    [job] = await scheduler.tick()
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE provider_operations SET state='failed',finished_at=clock_timestamp() WHERE id=:id"), {"id": job})
        await session.execute(text("UPDATE catalog_sources SET failure_count=2 WHERE id=:id"), {"id": source.id})
        await session.execute(text("UPDATE providers SET cooldown_until=clock_timestamp()+interval '2 hours' WHERE id=:id"), {"id": source.provider_id})
    await asyncio.gather(scheduler.completed(job), scheduler.completed(job))
    async with pg_db.sessions() as session:
        row = (await session.execute(text("SELECT failure_count,extract(epoch FROM next_run_at-cooldown_until) AS gap "
            "FROM catalog_sources s JOIN providers p ON p.id=s.provider_id WHERE s.id=:id"), {"id": source.id})).one()
    assert row.failure_count == 3
    assert row.gap == 17


async def test_enqueue_and_schedule_advance_rollback_together_and_other_source_survives(pg_db, monkeypatch):
    from autobuild_json.gateway.catalog.scheduler import Scheduler
    sources, bad = await source_case(pg_db)
    _, good = await source_case(pg_db)
    await due(pg_db, bad)
    await due(pg_db, good)
    ops = OperationStore(pg_db, sources)
    enqueue = ops.enqueue_in
    async def interrupted(session, request, actor, context):
        result = await enqueue(session, request, actor, context)
        if request.source_id == bad.id:
            raise RuntimeError("synthetic failure after insert")
        return result
    monkeypatch.setattr(ops, "enqueue_in", interrupted)
    assert len(await Scheduler(pg_db, ops, sources).tick()) == 1
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM provider_operations WHERE source_id=:id"), {"id": bad.id}) == 0
        assert await session.scalar(text("SELECT next_run_at<clock_timestamp() FROM catalog_sources WHERE id=:id"), {"id": bad.id})


async def test_locked_provider_does_not_lock_source_before_manual_update(pg_db):
    from autobuild_json.gateway.catalog.scheduler import Scheduler
    from autobuild_json.gateway.catalog.records import SourceInput
    sources, source = await source_case(pg_db)
    await due(pg_db, source)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("SELECT id FROM providers WHERE id=:id FOR UPDATE"), {"id": source.provider_id})
        scheduler = Scheduler(pg_db, OperationStore(pg_db, sources), sources)
        assert await asyncio.wait_for(scheduler.tick(), 2) == []
        # The skipping scheduler must not retain a source lock.
        await session.execute(text("SELECT id FROM catalog_sources WHERE id=:id FOR UPDATE NOWAIT"), {"id": source.id})
    await asyncio.wait_for(sources.update(source.id, source.version, SourceInput(provider_id=source.provider_id,
        credential_id=source.credential_id, mode=source.mode, schedule_enabled=True), "admin"), 2)


async def test_scheduler_and_manual_source_update_event_gate_no_deadlock(pg_db):
    from autobuild_json.gateway.catalog.scheduler import Scheduler
    from autobuild_json.gateway.catalog.records import SourceInput
    sources, source = await source_case(pg_db)
    await due(pg_db, source)
    scheduler = Scheduler(pg_db, OperationStore(pg_db, sources), sources)
    update = SourceInput(provider_id=source.provider_id, credential_id=source.credential_id,
                         mode=source.mode, schedule_enabled=True)
    results = await asyncio.wait_for(asyncio.gather(scheduler.tick(),
        sources.update(source.id, source.version, update, "admin")), 3)
    assert isinstance(results[0], list) and results[1].id == source.id
