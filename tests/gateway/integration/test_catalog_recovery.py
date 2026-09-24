from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.gateway.catalog_support import runner_case

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_repair_requires_deadline_grace_and_newer_fence(pg_db):
    from autobuild_json.gateway.catalog.worker import CatalogWorker
    async with runner_case(pg_db) as case:
        claim = await case.ops.claim(case.job.id, uuid4())
        worker = CatalogWorker(pg_db, case.runner, None)
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE provider_operations SET deadline=clock_timestamp()-interval '10 seconds',"
                "heartbeat_at=clock_timestamp()-interval '1 hour' WHERE id=:id"), {"id": case.job.id})
        assert await worker.repair_expired() == 0
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE provider_operations SET deadline=clock_timestamp()-interval '16 seconds' WHERE id=:id"), {"id": case.job.id})
        assert await worker.repair_expired() == 1
        assert await worker.repair_expired() == 0
        result = await case.ops.get(case.job.id)
        assert result.state == "failed" and result.error == "interrupted"
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT generation FROM provider_operations WHERE id=:id"), {"id": case.job.id}) > claim.generation
        assert not await case.ops.heartbeat(claim)


async def test_queued_ttl_cancelled_and_expired_probe_preserves_money(pg_db):
    from autobuild_json.gateway.catalog.worker import CatalogWorker
    async with runner_case(pg_db) as case:
        worker = CatalogWorker(pg_db, case.runner, None)
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE provider_operations SET queued_expires_at=clock_timestamp()-interval '1 second' WHERE id=:id"), {"id": case.job.id})
        assert await worker.repair_expired() == 1
        assert (await case.ops.get(case.job.id)).state == "cancelled"
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE provider_operations SET kind='probe',state='running',upper_cost=7,"
                "deadline=clock_timestamp()-interval '1 hour',dispatched_at=clock_timestamp() WHERE id=:id"), {"id": case.job.id})
        assert await worker.repair_expired() == 0
        async with pg_db.sessions() as session:
            row = (await session.execute(text("SELECT state,upper_cost,cost FROM provider_operations WHERE id=:id"), {"id": case.job.id})).one()
        assert row.state == "running" and row.upper_cost == 7 and row.cost is None
