import asyncio

import pytest
from sqlalchemy import text

from tests.gateway.catalog_support import runner_case

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_worker_shutdown_joins_real_runner_cleanup(pg_db):
    from autobuild_json.gateway.catalog.worker import CatalogWorker
    from autobuild_json.gateway.catalog.scheduler import Scheduler
    async with runner_case(pg_db, fixed=True) as case:
        case.pause_after_first_page.set()
        worker = CatalogWorker(pg_db, case.runner, Scheduler(pg_db, case.ops, case.sources))
        before = asyncio.all_tasks()
        stopped = asyncio.Event()
        task = asyncio.create_task(worker.run(stopped))
        await asyncio.wait_for(case.first_page_entered.wait(), 5)
        stopped.set()
        await asyncio.wait_for(task, 5)
        assert case.http_closed.is_set()
        assert (await case.ops.get(case.job.id)).state == "cancelled"
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0
            assert await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL")) == 0
        assert not (asyncio.all_tasks() - before)


async def test_maintenance_recovery_runs_while_two_real_jobs_stall(pg_db, monkeypatch):
    from contextlib import AsyncExitStack
    from autobuild_json.gateway.catalog.worker import CatalogWorker
    from autobuild_json.gateway.catalog.scheduler import Scheduler
    from autobuild_json.gateway.maintenance import Maintenance
    async with AsyncExitStack() as stack:
        cases = [await stack.enter_async_context(runner_case(pg_db)) for _ in range(3)]
        first = cases[0]
        # One real runner serves three distinct sources through synthetic I/O.
        entered, release = asyncio.Event(), asyncio.Event()
        active = set()
        collect = first.runner.discovery.collect
        async def stalled(context, *args, **kwargs):
            active.add(context.source.id)
            if len(active) == 2:
                entered.set()
            await release.wait()
            return await collect(context, *args, **kwargs)
        monkeypatch.setattr(first.runner.discovery, "collect", stalled)
        worker = CatalogWorker(pg_db, first.runner, Scheduler(pg_db, first.ops, first.sources))
        maintenance = Maintenance(pg_db, worker=worker)
        recovered = asyncio.Event()
        repair = worker.repair_expired
        async def observe_repair(*args, **kwargs):
            if entered.is_set():
                recovered.set()
            return await repair(*args, **kwargs)
        monkeypatch.setattr(worker, "repair_expired", observe_repair)
        wait_for = asyncio.wait_for
        async def fast_interval(awaitable, timeout):
            return await wait_for(awaitable, .02 if timeout == 15 else timeout)
        monkeypatch.setattr(asyncio, "wait_for", fast_interval)
        stopped = asyncio.Event()
        task = asyncio.create_task(maintenance.run(stopped))
        try:
            await wait_for(entered.wait(), 5)
            await wait_for(recovered.wait(), 5)
            async with pg_db.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM provider_operations WHERE state='running'")) == 2
                assert await session.scalar(text("SELECT count(*) FROM provider_operations WHERE state='queued'")) == 1
        finally:
            stopped.set()
            await wait_for(task, 5)


async def test_cli_composes_real_worker_and_closes_on_stop(pg_db):
    from autobuild_json.gateway.__main__ import run_maintenance
    from autobuild_json.gateway.admin.services import AdminServices
    from tests.gateway.catalog_support import synthetic_vault
    from autobuild_json.gateway.transport.http import Transport
    from autobuild_json.gateway.transport.egress import EgressPolicy
    import httpx
    async def resolver(host, port):
        return ["93.184.216.34"]
    services = AdminServices(pg_db, synthetic_vault(), b"s"*32,
        transport=Transport(EgressPolicy(resolver=resolver), adapter=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": []}))))
    stopped = asyncio.Event()
    stopped.set()
    await run_maintenance(services, stopped)
    assert services.catalog_worker.runner.ops is services.catalog_operations


async def test_cli_sigterm_stops_and_closes_on_same_loop(pg_db, monkeypatch):
    import signal
    from autobuild_json.gateway.__main__ import run_maintenance
    from autobuild_json.gateway.admin.services import AdminServices
    from tests.gateway.catalog_support import synthetic_vault
    services = AdminServices(pg_db, synthetic_vault(), b"s"*32)
    loop = asyncio.get_running_loop()
    handlers, removed = {}, []
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: handlers.__setitem__(sig, callback))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: removed.append(sig))
    close = services.close
    closed = asyncio.Event()
    async def observed_close():
        assert asyncio.get_running_loop() is loop
        await close()
        closed.set()
    monkeypatch.setattr(services, "close", observed_close)
    stopped = asyncio.Event()
    stopped.set()
    await run_maintenance(services, stopped)
    assert signal.SIGTERM in handlers
    assert signal.SIGINT in handlers
    assert set(removed) == set(handlers)
    assert closed.is_set()


async def test_recovery_independent_of_stalled_customer_maintenance_and_runs_retention(pg_db, monkeypatch):
    from autobuild_json.gateway.maintenance import Maintenance
    from autobuild_json.gateway.catalog.worker import CatalogWorker
    from autobuild_json.gateway.catalog.scheduler import Scheduler
    from autobuild_json.gateway.catalog.retention import Retention
    from tests.gateway.catalog_support import snapshot_case, complete_snapshot
    sources, source, store, old = await snapshot_case(pg_db, ())
    await complete_snapshot(store, sources, source.id, ())
    await complete_snapshot(store, sources, source.id, ())
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE provider_operations SET created_at=clock_timestamp()-interval '31 days' WHERE id=:id"), {"id": old})
    async with runner_case(pg_db) as case:
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE provider_operations SET queued_expires_at=clock_timestamp()-interval '1 second' WHERE id=:id"), {"id": case.job.id})
        worker = CatalogWorker(pg_db, case.runner, Scheduler(pg_db, case.ops, case.sources))
        maintenance = Maintenance(pg_db, worker=worker)
        entered, purged = asyncio.Event(), asyncio.Event()
        async def stalled_tick():
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(maintenance, "tick", stalled_tick)
        purge = Retention.purge
        async def observed_purge(self, *args, **kwargs):
            result = await purge(self, *args, **kwargs)
            purged.set()
            return result
        monkeypatch.setattr(Retention, "purge", observed_purge)
        stopped = asyncio.Event()
        task = asyncio.create_task(maintenance.run(stopped))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(purged.wait(), 2)
            assert (await case.ops.get(case.job.id)).state == "cancelled"
            async with pg_db.sessions() as session:
                assert not await session.scalar(text("SELECT EXISTS(SELECT 1 FROM provider_operations WHERE id=:id)"), {"id": old})
        finally:
            stopped.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_no_probe_handler_preserves_evidence_but_fails_clean_probe(pg_db):
    from autobuild_json.gateway.catalog.worker import CatalogWorker
    async with runner_case(pg_db) as case:
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE provider_operations SET kind='probe',dispatched_at=clock_timestamp(),upper_cost=7 WHERE id=:id"), {"id": case.job.id})
        worker = CatalogWorker(pg_db, case.runner, None)
        await worker._execute(case.job.id)
        assert (await case.ops.get(case.job.id)).state == "queued"
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE provider_operations SET dispatched_at=NULL,upper_cost=NULL WHERE id=:id"), {"id": case.job.id})
        await worker._execute(case.job.id)
        job = await case.ops.get(case.job.id)
        assert job.state == "failed" and job.dispatched_at is None and job.cost is None
        assert case.sent_pages == 0


async def test_stop_wakes_worker_during_blocked_scheduler(pg_db, monkeypatch):
    from autobuild_json.gateway.catalog.worker import CatalogWorker
    from autobuild_json.gateway.catalog.scheduler import Scheduler
    async with runner_case(pg_db) as case:
        scheduler = Scheduler(pg_db, case.ops, case.sources)
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def stalled():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        monkeypatch.setattr(scheduler, "tick", stalled)
        stopped = asyncio.Event()
        task = asyncio.create_task(CatalogWorker(pg_db, case.runner, scheduler).run(stopped))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            stopped.set()
            await asyncio.wait_for(asyncio.shield(task), 2)
            assert cleaned.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancellation_joins_scheduler_and_stop_helpers(pg_db, monkeypatch):
    from autobuild_json.gateway.catalog.worker import CatalogWorker
    from autobuild_json.gateway.catalog.scheduler import Scheduler
    async with runner_case(pg_db) as case:
        scheduler = Scheduler(pg_db, case.ops, case.sources)
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def stalled():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        monkeypatch.setattr(scheduler, "tick", stalled)
        worker = CatalogWorker(pg_db, case.runner, scheduler)
        stopped = asyncio.Event()
        before = asyncio.all_tasks()
        task = asyncio.create_task(worker.run(stopped))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()
        assert not (asyncio.all_tasks() - before)


async def test_repeated_scheduler_failures_join_helpers_each_iteration(pg_db, monkeypatch):
    from autobuild_json.gateway.catalog.worker import CatalogWorker
    from autobuild_json.gateway.catalog.scheduler import Scheduler
    async with runner_case(pg_db) as case:
        scheduler = Scheduler(pg_db, case.ops, case.sources)
        stopped = asyncio.Event()
        worker = CatalogWorker(pg_db, case.runner, scheduler)
        async def no_repair(*args, **kwargs):
            return 0
        monkeypatch.setattr(worker, "repair_expired", no_repair)
        calls = 0
        task = None
        async def failing_tick():
            nonlocal calls
            calls += 1
            assert worker._helper_tasks and all(not helper.done() for helper in worker._helper_tasks)
            if calls == 3:
                stopped.set()
            raise RuntimeError("synthetic scheduler failure")
        monkeypatch.setattr(scheduler, "tick", failing_tick)
        before = asyncio.all_tasks()
        task = asyncio.create_task(worker.run(stopped))
        await asyncio.wait_for(task, 5)
        assert calls == 3
        assert worker._helper_tasks == set()
        assert not (asyncio.all_tasks() - before)
