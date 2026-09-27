"""Server-owned quota refresh. No provider work from GETs or until opted in.

A PostgreSQL session lock fences the entire run (including provider cleanup)
across admin processes. A lost worker's run is interrupted, never replayed.
"""
import asyncio
from contextlib import suppress
from uuid import uuid4

from sqlalchemy import text

from ...oauth import ISSUER
from ..errors import GatewayError


async def _join_workers(worker, count):
    """Run bounded workers on every supported Python version.

    ``asyncio.TaskGroup`` was added in Python 3.11, while this package still
    supports 3.10.  ``gather`` plus explicit sibling cancellation preserves
    the failure/cancellation semantics needed by the refresh lock without
    making the feature silently fail on 3.10.
    """
    tasks = [asyncio.create_task(worker()) for _ in range(count)]
    group = asyncio.gather(*tasks)
    try:
        await asyncio.shield(group)
    except BaseException as exc:
        for task in tasks:
            if not task.done():
                task.cancel()
        cleanup = asyncio.gather(group, *tasks, return_exceptions=True)
        cancelled = isinstance(exc, asyncio.CancelledError)
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError
        raise


async def _release_advisory_lock(connection):
    """Release the session lock before a pooled connection is returned.

    If the unlock cannot be confirmed, invalidate the connection so a session
    lock cannot leak into the next request that borrows it.  Cancellation is
    re-raised after invalidation so the caller keeps its cancellation state.
    """
    try:
        await connection.execute(text("SELECT pg_advisory_unlock(hashtext(current_schema()),731459)"))
        await connection.commit()
    except asyncio.CancelledError:
        with suppress(Exception):
            await connection.invalidate()
        raise
    except Exception:
        with suppress(Exception):
            await connection.invalidate()


class QuotaRefreshJobs:
    def __init__(self, services):
        self.services, self.db = services, services.db
        self.wake = asyncio.Event()
        self.task = None

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def read(self):
        async with self.db.sessions.begin() as session:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            settings = (await session.execute(text("SELECT version,interval_minutes,next_run_at "
                "FROM codex_quota_refresh_settings"))).mappings().first()
            job = (await session.execute(text("SELECT * FROM codex_quota_refresh_runs "
                "ORDER BY created_at DESC,id DESC LIMIT 1"))).mappings().first()
        return {"settings": dict(settings) if settings else {"version": 0, "interval_minutes": 0, "next_run_at": None},
                "job": dict(job) if job else None, "concurrency": 2}

    async def _lock_settings(self, session):
        created = await session.scalar(text("INSERT INTO codex_quota_refresh_settings(singleton) VALUES (true) "
                                             "ON CONFLICT DO NOTHING RETURNING singleton"))
        row = (await session.execute(text("SELECT * FROM codex_quota_refresh_settings FOR UPDATE"))).mappings().one()
        return row, bool(created)

    async def configure(self, payload):
        async with self.db.sessions.begin() as session:
            # Absent row is externally version 0; creation is rolled back on a
            # bad CAS so a failed save cannot silently enable background work.
            row, created = await self._lock_settings(session)
            if payload.version != (0 if created else row["version"]):
                raise GatewayError("version_conflict", 409)
            active = await session.scalar(text("SELECT EXISTS(SELECT 1 FROM codex_quota_refresh_runs "
                                                "WHERE state IN ('queued','running'))"))
            await session.execute(text("""UPDATE codex_quota_refresh_settings SET version=CASE WHEN :created THEN 1 ELSE version+1 END,
                interval_minutes=:minutes,next_run_at=CASE WHEN :minutes=0 OR :active THEN NULL
                ELSE clock_timestamp()+make_interval(mins=>:minutes) END"""),
                {"minutes": payload.interval_minutes, "active": active, "created": created})
            await self.services.identity._audit(session, "codex.quota_refresh.configured", uuid4(),
                {"interval_minutes": payload.interval_minutes})
        self.wake.set()
        return await self.read()

    async def enqueue(self, request_id):
        async with self.db.sessions.begin() as session:
            await self._lock_settings(session)
            # Idempotent submission and multiple tabs coalesce to one active run.
            existing = await session.scalar(text("SELECT id FROM codex_quota_refresh_runs WHERE id=:id"), {"id": request_id})
            active = await session.scalar(text("SELECT id FROM codex_quota_refresh_runs WHERE state IN ('queued','running')"))
            if existing is None and active is None:
                await session.execute(text("INSERT INTO codex_quota_refresh_runs(id,trigger,state) VALUES (:id,'manual','queued')"), {"id": request_id})
                await session.execute(text("UPDATE codex_quota_refresh_settings SET next_run_at=NULL"))
                await self.services.identity._audit(session, "codex.quota_refresh.queued", request_id)
        self.wake.set()
        return await self.read()

    async def stop(self, request_id):
        async with self.db.sessions.begin() as session:
            changed = await session.scalar(text("UPDATE codex_quota_refresh_runs SET stop_requested=true "
                "WHERE id=:id AND state IN ('queued','running') RETURNING id"), {"id": request_id})
            if changed:
                await self.services.identity._audit(session, "codex.quota_refresh.stop_requested", request_id)
        self.wake.set()
        return await self.read()

    async def _finish(self, identity, state, code=None):
        async with self.db.sessions.begin() as session:
            await self._lock_settings(session)
            await session.execute(text("UPDATE codex_quota_refresh_runs SET state=:state,result_code=:code,"
                "finished_at=clock_timestamp() WHERE id=:id AND state IN ('running','queued')"),
                {"id": identity, "state": state, "code": code})
            await session.execute(text("UPDATE codex_quota_refresh_settings SET next_run_at=CASE "
                "WHEN interval_minutes=0 THEN NULL ELSE clock_timestamp()+make_interval(mins=>interval_minutes) END"))

    async def tick(self):
        # The connection is pooled.  Always unlock before returning it, and
        # invalidate it if the unlock cannot be confirmed.
        async with self.db.engine.connect() as connection:
            lock_state = "acquiring"
            try:
                locked = await connection.scalar(text("SELECT pg_try_advisory_lock(hashtext(current_schema()),731459)"))
                await connection.commit()
                lock_state = "locked" if locked else "free"
                if not locked:
                    return
                async with self.db.sessions() as session:
                    orphan = await session.scalar(text("SELECT id FROM codex_quota_refresh_runs WHERE state='running'"))
                if orphan is not None:
                    await self._finish(orphan, "interrupted", "worker_interrupted")
                async with self.db.sessions.begin() as session:
                    settings = (await session.execute(text("SELECT * FROM codex_quota_refresh_settings FOR UPDATE"))).mappings().first()
                    if settings is None:
                        return
                    job = (await session.execute(text("SELECT * FROM codex_quota_refresh_runs WHERE state='queued'"))).mappings().first()
                    if job is None and settings["interval_minutes"] > 0:
                        due = await session.scalar(text("SELECT next_run_at<=clock_timestamp() FROM codex_quota_refresh_settings"))
                        if due:
                            job = (await session.execute(text("INSERT INTO codex_quota_refresh_runs(id,trigger,state) "
                                "VALUES (:id,'automatic','queued') RETURNING *"), {"id": uuid4()})).mappings().one()
                    if job is None:
                        return
                    await session.execute(text("UPDATE codex_quota_refresh_runs SET state='running',started_at=clock_timestamp() WHERE id=:id"), {"id": job["id"]})
                    await session.execute(text("UPDATE codex_quota_refresh_settings SET next_run_at=NULL"))
                await self._execute(job["id"])
            finally:
                if lock_state == "locked":
                    await _release_advisory_lock(connection)
                elif lock_state == "acquiring":
                    # Cancellation or a driver error can arrive after
                    # PostgreSQL acquired the lock but before the result was
                    # assigned locally. Never return that unknown session to
                    # the pool.
                    with suppress(Exception):
                        await connection.invalidate()

    async def _execute(self, identity):
        from ..admin.codex_accounts import AccountViews
        views = AccountViews(self.services)
        try:
            async with self.db.sessions.begin() as session:
                rows = (await session.execute(text("""SELECT c.id,c.email,c.enabled FROM credentials c
                    JOIN providers p ON p.id=c.provider_id WHERE c.issuer=:issuer AND c.account_id IS NOT NULL
                    AND c.account_id<>'' AND p.config->>'adapter'='codex_oauth' AND p.config->>'auth_mode'='oauth'
                    ORDER BY c.id"""), {"issuer": ISSUER})).mappings().all()
                await session.execute(text("UPDATE codex_quota_refresh_runs SET total=:total WHERE id=:id"),
                                      {"total": len(rows), "id": identity})
            iterator, rate_limited = iter(rows), False

            async def worker():
                nonlocal rate_limited
                for row in iterator:
                    async with self.db.sessions() as session:
                        stopped = await session.scalar(text("SELECT stop_requested FROM codex_quota_refresh_runs WHERE id=:id"), {"id": identity})
                    if stopped or rate_limited:
                        return
                    error = None
                    if not row["enabled"]:
                        outcome = "skipped"
                    else:
                        try:
                            result = await views.refresh(row["id"])
                            failed = [kind for kind in ("usage", "credits") if result[kind + "_status"] == "failed"]
                            outcome = "failed" if failed else "succeeded"
                            if failed:
                                error = "codex_" + failed[0] + "_unavailable"
                        except GatewayError as exc:
                            outcome, error = "failed", exc.code
                            if exc.status == 429:
                                rate_limited = True
                        # Only domain codes leave this worker, never raw exceptions.
                    async with self.db.sessions.begin() as session:
                        await session.execute(text(f"""UPDATE codex_quota_refresh_runs SET {outcome}={outcome}+1,
                            errors=CASE WHEN CAST(:error AS text) IS NOT NULL AND jsonb_array_length(errors)<100
                                THEN errors||jsonb_build_array(jsonb_build_object('credential_id',CAST(:account AS text),
                                    'email',CAST(:email AS text),'code',CAST(:error AS text)))
                                ELSE errors END WHERE id=:id AND state='running'"""),
                                {"id": identity, "account": str(row["id"]), "email": row["email"], "error": error})

            # Join/cancel both workers before releasing the DB lock.
            await _join_workers(worker, 2)
            async with self.db.sessions() as session:
                stopped = await session.scalar(text("SELECT stop_requested FROM codex_quota_refresh_runs WHERE id=:id"), {"id": identity})
            await self._finish(identity, "stopped" if stopped or rate_limited else "completed",
                               "rate_limited" if rate_limited else None)
        except asyncio.CancelledError:
            with suppress(Exception):
                await self._finish(identity, "interrupted", "worker_interrupted")
            raise
        except Exception:
            await self._finish(identity, "failed", "quota_refresh_failed")

    async def run(self):
        while True:
            self.wake.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Storage unavailable: do not dispatch; wait for the next pass.
                pass
            try:
                await asyncio.wait_for(self.wake.wait(), 2)
            except asyncio.TimeoutError:
                pass
