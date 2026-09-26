from datetime import datetime, timezone, timedelta
from uuid import uuid4

import pytest

from .test_ledger import ledger_case, bucket

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_recovery_keeps_unknown_holds_and_releases_never_dispatched(pg_db):
    from sqlalchemy import text
    from autobuild_json.gateway.maintenance import Maintenance
    ledger, admission, issued, _ = await ledger_case(pg_db)
    pending, unspent = admission(500), admission(100)
    await ledger.reserve(pending)
    await ledger.mark_dispatched(pending.request_id, uuid4())
    await ledger.reserve(unspent)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE requests SET deadline=:past"), {"past": datetime.now(timezone.utc)-timedelta(seconds=30)})
    await Maintenance(pg_db).tick()
    assert await bucket(pg_db, issued.key_id) == (0, 500)
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT state FROM requests WHERE id=:id"), {"id": pending.request_id}) == "usage_pending"
        assert await session.scalar(text("SELECT state FROM requests WHERE id=:id"), {"id": unspent.request_id}) == "released"


@pytest.mark.parametrize("pending", [False, True])
async def test_recovery_can_finalize_saved_authoritative_usage_once(pg_db, pending):
    from sqlalchemy import text
    from autobuild_json.gateway.maintenance import Maintenance
    ledger, admission, issued, _ = await ledger_case(pg_db)
    request, attempt = admission(500), uuid4()
    await ledger.reserve(request)
    await ledger.mark_dispatched(request.request_id, attempt)
    if pending:
        await ledger.mark_pending(request.request_id, "interrupted")
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE attempts SET usage=CAST(:usage AS jsonb),status='completed' WHERE id=:id"),
                              {"id": attempt, "usage": '{"input_tokens":10,"output_tokens":5}'})
        await session.execute(text("UPDATE requests SET deadline=:past"), {"past": datetime.now(timezone.utc)-timedelta(seconds=30)})
    await Maintenance(pg_db).tick()
    await Maintenance(pg_db).tick()
    assert await bucket(pg_db, issued.key_id) == (15, 0)


async def test_unknown_usage_does_not_fill_every_recovery_batch(pg_db):
    from sqlalchemy import text
    from autobuild_json.gateway.maintenance import Maintenance

    ledger, admission, issued, _ = await ledger_case(pg_db, concurrency=200, rpm=200)
    for _ in range(105):
        request = admission(1)
        await ledger.reserve(request)
        await ledger.mark_dispatched(request.request_id, uuid4())
        await ledger.mark_pending(request.request_id, "usage_missing")
    unspent = admission(100)
    await ledger.reserve(unspent)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE requests SET deadline=now()-interval '30 seconds'"))

    maintenance = Maintenance(pg_db)
    # Only the never-dispatched request is actionable. Unknown provider usage
    # must keep its holds without repeatedly occupying the bounded work queue.
    assert await maintenance.tick() == 1
    assert await bucket(pg_db, issued.key_id) == (0, 105)
    assert await maintenance.tick() == 0
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM requests WHERE state='usage_pending' "
                                         "AND reason='usage_missing'")) == 105
