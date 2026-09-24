"""Two owned execution slots, independent from maintenance's ledger lease."""

import asyncio
from uuid import uuid4

from sqlalchemy import text


class CatalogWorker:
    def __init__(self, db, runner, scheduler, probe_accounting=None):
        self.db, self.runner, self.scheduler = db, runner, scheduler
        self.probe_accounting = probe_accounting

    async def _execute(self, identity):
        try:
            async with self.db.sessions() as session:
                row = (await session.execute(text("SELECT kind,dispatched_at,budget_id,upper_cost,usage,cost FROM provider_operations WHERE id=:id"), {"id": identity})).mappings().first()
            if row is not None and row["kind"] == "probe" and self.probe_accounting is None:
                # Never guess about an evidence-bearing probe. A clean, undispatched
                # probe can be terminally rejected without touching money columns.
                if row["dispatched_at"] is not None or row["budget_id"] is not None or row["upper_cost"] is not None or row["usage"] is not None or row["cost"] is not None:
                    return
                async with self.db.sessions.begin() as session:
                    await session.execute(text("UPDATE provider_operations SET state='failed',error_code='unsupported_feature',finished_at=clock_timestamp(),version=version+1 WHERE id=:id AND state='queued'"), {"id": identity})
                return
            await self.runner.run(identity)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Uncertain rows remain fenced and repairable after deadline + grace.
            pass
        if self.scheduler is not None:
            try:
                await self.scheduler.completed(identity)
            except Exception:
                pass

    async def run(self, stopped: asyncio.Event) -> None:
        tasks = {}
        helpers = set()
        self._helper_tasks = helpers
        next_repair = 0.0
        loop = asyncio.get_running_loop()
        try:
            while not stopped.is_set():
                for task in tuple(tasks):
                    if task.done():
                        await asyncio.gather(task, return_exceptions=True)
                        del tasks[task]
                try:
                    if loop.time() >= next_repair:
                        await self.repair_expired()
                        next_repair = loop.time() + 15
                    if self.scheduler is not None:
                        if await self._scheduler_pass(stopped, helpers):
                            break
                    if len(tasks) < 2:
                        async with self.db.sessions() as session:
                            queued = (await session.execute(text("SELECT id FROM provider_operations WHERE state='queued' "
                                "AND queued_expires_at>clock_timestamp() ORDER BY created_at,id LIMIT 2"))).scalars().all()
                        for identity in queued:
                            if identity not in tasks.values() and len(tasks) < 2 and not stopped.is_set():
                                tasks[asyncio.create_task(self._execute(identity))] = identity
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stopped.wait(), 1)
                except asyncio.TimeoutError:
                    pass
        finally:
            for helper in helpers:
                helper.cancel()
            helper_cleanup = asyncio.gather(*helpers, return_exceptions=True)
            while not helper_cleanup.done():
                try:
                    await asyncio.shield(helper_cleanup)
                except asyncio.CancelledError:
                    continue
            await helper_cleanup
            for task in tasks:
                task.cancel()
            # Task7 runner owns a shared <=5s cancellation/cleanup budget.
            # Shield and join it even if the caller is cancelled a second time.
            cleanup = asyncio.gather(*tasks, return_exceptions=True)
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            await cleanup
            self._helper_tasks = set()

    async def _scheduler_pass(self, stopped, helpers) -> bool:
        """Run one bounded scheduler tick and always join both helper tasks."""
        tick = asyncio.create_task(self.scheduler.tick())
        stop_wait = asyncio.create_task(stopped.wait())
        helpers.update((tick, stop_wait))
        try:
            done, pending = await asyncio.wait({tick, stop_wait}, timeout=1,
                                               return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if not done:
                return False
            if stop_wait in done:
                return True
            await tick
            return False
        finally:
            for helper in (tick, stop_wait):
                if not helper.done():
                    helper.cancel()
            await asyncio.gather(tick, stop_wait, return_exceptions=True)
            helpers.discard(tick)
            helpers.discard(stop_wait)

    async def repair_expired(self, limit=100) -> int:
        limit = max(0, min(limit, 100))
        async with self.db.sessions.begin() as session:
            rows = (await session.execute(text("SELECT * FROM provider_operations WHERE "
                "(state='queued' AND queued_expires_at<=clock_timestamp()) OR "
                "(state='running' AND deadline<clock_timestamp()-interval '15 seconds') "
                "ORDER BY created_at,id LIMIT :limit FOR UPDATE SKIP LOCKED"), {"limit": limit})).mappings().all()
            repaired, probes = [], []
            for row in rows:
                if row["kind"] == "probe" and (row["state"] == "running" or row["budget_id"] is not None
                        or row["upper_cost"] is not None or row["dispatched_at"] is not None):
                    probes.append(row["id"])
                    continue
                await session.execute(text("UPDATE provider_operations SET state=:state,error_code=:error, "
                    "generation=generation+1,owner=:owner,finished_at=clock_timestamp(),version=version+1 WHERE id=:id"),
                    {"id": row["id"], "state": "cancelled" if row["state"] == "queued" else "failed",
                     "error": "operation_expired" if row["state"] == "queued" else "interrupted", "owner": uuid4()})
                repaired.append(row["id"])
        for identity in repaired:
            if self.scheduler is not None:
                await self.scheduler.completed(identity)
        count = len(repaired)
        if self.probe_accounting is not None:
            for identity in probes:
                # Handler rechecks expiry/fence and owns accounting locks/transaction.
                recover = getattr(self.probe_accounting, "recover", None)
                if recover is None:
                    recover = self.probe_accounting.recover_expired
                count += bool(await recover(identity))
        try:
            from .retention import Retention
            await Retention(self.db).purge(__import__("datetime").datetime.now(__import__("datetime").timezone.utc), limit=100)
        except Exception:
            pass
        return count
