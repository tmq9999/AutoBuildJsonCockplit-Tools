"""Bounded, database-clock scheduling in the shared configuration lock order."""

from datetime import timedelta
import random
from uuid import UUID, uuid4

from sqlalchemy import text

from .records import OperationRequest


class Scheduler:
    def __init__(self, db, ops, sources, jitter=random.randint):
        self.db, self.ops, self.sources, self.jitter = db, ops, sources, jitter

    async def tick(self) -> list[UUID]:
        async with self.db.sessions() as session:
            pending = (await session.execute(text("SELECT id FROM provider_operations WHERE kind='discover' "
                "AND state NOT IN ('queued','running','usage_pending') AND schedule_completed_at IS NULL "
                "ORDER BY finished_at,id LIMIT 100"))).scalars().all()
        for identity in pending:
            try:
                await self.completed(identity)
            except Exception:
                # Retry durable acknowledgement next tick; never log secrets from drivers.
                pass
        async with self.db.sessions() as session:
            candidates = (await session.execute(text("SELECT id FROM catalog_sources WHERE enabled "
                "AND schedule_enabled AND next_run_at<=clock_timestamp() ORDER BY next_run_at,id LIMIT 100"))).scalars().all()
        queued = []
        for identity in candidates:
            try:
                async with self.db.sessions.begin() as session:
                    context = await self.sources.lock_context(session, identity, skip_locked=True)
                    if context is None:
                        continue
                    row = (await session.execute(text("SELECT s.*,clock_timestamp() AS db_now, "
                        "GREATEST(p.cooldown_until,c.cooldown_until) AS cooldown FROM catalog_sources s "
                        "JOIN providers p ON p.id=s.provider_id JOIN credentials c ON c.id=s.credential_id "
                        "WHERE s.id=:id"), {"id": identity})).mappings().one()
                    now = row["db_now"]
                    if (not row["enabled"] or not row["schedule_enabled"] or row["next_run_at"] is None
                            or row["next_run_at"] > now or (row["cooldown"] and row["cooldown"] > now)):
                        continue
                    if await session.scalar(text("SELECT EXISTS(SELECT 1 FROM provider_operations "
                        "WHERE source_id=:id AND state IN ('queued','running'))"), {"id": identity}):
                        continue
                    operation = await self.ops.enqueue_in(session, OperationRequest(id=uuid4(), source_id=identity,
                        kind="discover", expected=context.stamp), "scheduler", context)
                    await session.execute(text("UPDATE catalog_sources SET next_run_at=:next WHERE id=:id"),
                        {"id": identity, "next": now + timedelta(seconds=row["interval_seconds"] + self.jitter(0, 60))})
                queued.append(operation.id)
            except Exception:
                # One broken source cannot starve other providers; transaction rolls back.
                continue
        return queued

    async def completed(self, operation_id: UUID) -> None:
        async with self.db.sessions.begin() as session:
            identity = (await session.execute(text("SELECT s.id,s.provider_id,s.credential_id FROM catalog_sources s "
                "JOIN provider_operations o ON o.source_id=s.id WHERE o.id=:id"), {"id": operation_id})).mappings().first()
            if identity is None:
                return
            # Disabled sources still need acknowledgement, using the same global order.
            await self.sources._dependencies(session, identity["provider_id"], identity["credential_id"],
                                             require_healthy=False, allow_missing_profile=True)
            source = (await session.execute(text("SELECT * FROM catalog_sources WHERE id=:id FOR UPDATE"),
                                            {"id": identity["id"]})).mappings().one()
            operation = (await session.execute(text("SELECT *,clock_timestamp() AS db_now FROM provider_operations "
                "WHERE id=:id FOR UPDATE"), {"id": operation_id})).mappings().first()
            if (operation is None or operation["state"] in {"queued", "running", "usage_pending"}
                    or operation["schedule_completed_at"] is not None):
                return
            if operation["kind"] == "discover":
                # A delayed acknowledgement must not reschedule a newer attempt.
                newer = await session.scalar(text("SELECT EXISTS(SELECT 1 FROM provider_operations WHERE source_id=:source "
                    "AND kind='discover' AND (created_at,id)>(:created,:id))"),
                    {"source": identity["id"], "created": operation["created_at"], "id": operation_id})
                if not newer:
                    failures = 0 if operation["state"] == "succeeded" else source["failure_count"] + 1
                    now = operation["db_now"]
                    cooldown = await session.scalar(text("SELECT GREATEST(p.cooldown_until,c.cooldown_until) "
                        "FROM providers p JOIN credentials c ON c.provider_id=p.id WHERE c.id=:id"),
                        {"id": identity["credential_id"]})
                    backoff = 0 if failures == 0 else (300, 900, 3600)[min(failures, 3)-1]
                    next_run = max(now + timedelta(seconds=source["interval_seconds"]),
                                   now + timedelta(seconds=backoff), cooldown or now)
                    next_run += timedelta(seconds=self.jitter(0, 60))
                    await session.execute(text("UPDATE catalog_sources SET failure_count=:failures, "
                        "next_run_at=CASE WHEN enabled AND schedule_enabled THEN :next ELSE NULL END WHERE id=:id"),
                        {"id": identity["id"], "failures": failures, "next": next_run})
            await session.execute(text("UPDATE provider_operations SET schedule_completed_at=clock_timestamp() WHERE id=:id"),
                                  {"id": operation_id})
