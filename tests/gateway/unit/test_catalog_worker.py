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
