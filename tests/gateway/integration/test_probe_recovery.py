import pytest
from sqlalchemy import text

from tests.gateway.catalog_support import probe_case

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_worker_recovery_fences_expired_probe_and_keeps_unknown_hold(pg_db):
    from uuid import uuid4
    from autobuild_json.gateway.catalog.worker import CatalogWorker

    async with probe_case(pg_db) as case:
        operation = await case.ops.enqueue(case.command, "admin")
        claim = await case.ops.claim(operation.id, uuid4())
        await case.accounting.reserve(claim, case.quote)
        await case.accounting.mark_dispatched(claim)
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE provider_operations SET deadline=clock_timestamp()-interval '16 seconds' WHERE id=:id"),
                                  {"id": operation.id})
        worker = CatalogWorker(pg_db, case.runner, None, case.runner.probe)
        assert await worker.repair_expired() == 1
        view = await case.ops.get(operation.id)
        assert view.state == "usage_pending" and view.cost is None
        async with pg_db.sessions() as session:
            row = (await session.execute(text("SELECT generation,owner FROM provider_operations WHERE id=:id"),
                                         {"id": operation.id})).one()
            assert row.generation > claim.generation and row.owner != claim.owner
