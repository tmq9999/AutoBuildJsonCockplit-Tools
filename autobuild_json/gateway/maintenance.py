import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import text

from .metering.ledger import Ledger
from .metering.records import Usage
from .metering.budgets import BudgetService
from decimal import Decimal
from .proxy.pg_leases import PgLeaseStore


def recover_request(state, dispatched):
    if state in {"completed", "released", "adjusted"}:
        return "unchanged"
    return "usage_pending" if dispatched else "release_unspent"


class Maintenance:
    def __init__(self, db, worker=None):
        self.db, self.leases, self.ledger, self.worker = db, PgLeaseStore(db), Ledger(db), worker

    async def tick(self):
        lease = await self.leases.claim("maintenance:gateway", uuid4(), datetime.now(timezone.utc)+timedelta(seconds=30))
        if lease is None:
            return 0
        processed = 0
        try:
            async with self.db.sessions() as session:
                rows = (await session.execute(text("SELECT id,state FROM requests WHERE "
                    "(state IN ('reserved','dispatched','usage_pending') OR (state='released' AND EXISTS "
                    "(SELECT 1 FROM upstream_reservations b WHERE b.request_id=requests.id AND b.state<>'settled'))) "
                    "AND deadline<now()-interval '15 seconds' LIMIT 100"))).mappings().all()
            for row in rows:
                if not await self.leases.assert_owner(lease):
                    break
                if row["state"] in {"reserved","released"}:
                    await self.ledger.release_unspent(row["id"], "not_dispatched")
                    async with self.db.sessions() as session:
                        unused=(await session.execute(text("SELECT b.attempt_id FROM upstream_reservations b LEFT JOIN attempts a ON a.id=b.attempt_id "
                            "WHERE b.request_id=:id AND b.state<>'settled' AND (a.id IS NULL OR a.status='rejected')"),{'id':row['id']})).scalars().all()
                    for attempt in unused:
                        await BudgetService(self.db).settle(attempt,Decimal(0))
                else:
                    async with self.db.sessions() as session:
                        evidence = (await session.execute(text("SELECT usage FROM attempts WHERE request_id=:id "
                            "AND status='completed' AND usage IS NOT NULL ORDER BY started_at DESC LIMIT 1"), {"id": row["id"]})).scalar_one_or_none()
                    if evidence is not None:
                        await self.ledger.settle(row["id"], Usage(**evidence))
                    else:
                        await self.ledger.mark_pending(row["id"], "interrupted")
                processed += 1
            async with self.db.sessions.begin() as session:
                await session.execute(text("DELETE FROM continuation_handles WHERE expires_at<now()"))
                # Retain referenced accounting keys/usage; only incidental reasons
                # and expired dedup hashes are removed, not the immutable ledger.
                await session.execute(text("UPDATE requests SET payload_digest=NULL,idempotency_digest=NULL "
                                           "WHERE idempotency_expires_at<now()"))
            return processed
        finally:
            await self.leases.release(lease)

    async def run(self, stopped):
        worker_task = asyncio.create_task(self.worker.run(stopped)) if self.worker is not None else None
        try:
            while not stopped.is_set():
                try:
                    await self.tick()
                except Exception:
                    # Hold state stays in PostgreSQL for another pass. Do not log DB
                    # driver exception strings that may contain connection credentials.
                    pass
                if self.worker is not None:
                    try:
                        await self.worker.repair_expired()
                    except Exception:
                        pass
                    try:
                        from .catalog.retention import Retention
                        await Retention(self.db).purge(datetime.now(timezone.utc), limit=100)
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(stopped.wait(), 15)
                except asyncio.TimeoutError:
                    pass
        finally:
            if worker_task is not None:
                stopped.set()
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
