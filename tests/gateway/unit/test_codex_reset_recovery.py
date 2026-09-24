"""Recovery exercises PostgreSQL because its fence is the behavior under test."""
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.gateway.integration.test_codex_quota_store import seeded, deadline

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.mark.parametrize('dispatched,state', [(False, 'rejected'), (True, 'unknown')])
async def test_maintenance_recovers_only_storage_and_fences_old_worker(pg_db, dispatched, state):
    from autobuild_json.gateway.maintenance import Maintenance
    store, credential, credits = await seeded(pg_db)
    claim = await store.prepare_reset(credential, uuid4(), credits.version, 'admin', deadline=deadline())
    if dispatched:
        assert await store.mark_dispatched(claim.result.operation_id, claim.generation)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE codex_reset_requests SET requested_at=now()-interval '2 minutes',deadline=now()-interval '1 minute'"))
    assert await Maintenance(pg_db).tick() == 1
    assert await Maintenance(pg_db).tick() == 0
    assert (await store.get_reset(claim.result.operation_id)).state == state
    assert not await store.mark_dispatched(claim.result.operation_id, claim.generation)
