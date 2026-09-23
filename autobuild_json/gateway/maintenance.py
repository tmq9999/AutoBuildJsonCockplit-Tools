import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import text

from .metering.ledger import Ledger
from .metering.records import Usage
from .proxy.pg_leases import PgLeaseStore


def recover_request(state, dispatched):
    if state in {"completed", "released", "adjusted"}:
        return "unchanged"
    return "usage_pending" if dispatched else "release_unspent"


class Maintenance:
    def __init__(self, db):
        self.db, self.leases, self.ledger = db, PgLeaseStore(db), Ledger(db)

    async def tick(self):
        lease = await self.leases.claim("maintenance:gateway", uuid4(), datetime.now(timezone.utc)+timedelta(seconds=30))
        if lease is None:
            return 0
        processed = 0
        try:
            async with self.db.sessions() as session:
                rows = (await session.execute(text("SELECT id,state FROM requests WHERE "
                    "state IN ('reserved','dispatched','usage_pending') AND deadline<now()-interval '15 seconds' LIMIT 100"))).mappings().all()
            for row in rows:
                if not await self.leases.assert_owner(lease):
                    break
                if row["state"] == "reserved":
                    await self.ledger.release_unspent(row["id"], "not_dispatched")
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
        while not stopped.is_set():
            try:
                await self.tick()
            except Exception:
                # Hold state stays in PostgreSQL for another pass. Do not log DB
                # driver exception strings that may contain connection credentials.
                pass
            try:
                await asyncio.wait_for(stopped.wait(), 15)
            except asyncio.TimeoutError:
                pass
