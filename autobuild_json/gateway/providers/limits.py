import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from math import ceil
from uuid import uuid4

from sqlalchemy import text

from ..errors import GatewayError


class ProviderLimits:
    def __init__(self, db):
        self.db = db
        self._released = asyncio.Condition()
        self._wait_attempts = 0
        self._expired_waits = 0

    async def _try_acquire(self, route, deadline, identity):
        async with self.db.sessions.begin() as session:
            provider = (
                (
                    await session.execute(
                        text("SELECT config,version,cooldown_until FROM providers WHERE id=:id FOR UPDATE"),
                        {"id": route.provider_id},
                    )
                )
                .mappings()
                .first()
            )
            credential = (
                (
                    await session.execute(
                        text("SELECT * FROM credentials WHERE id=:id AND provider_id=:provider FOR UPDATE"),
                        {"id": route.credential_id, "provider": route.provider_id},
                    )
                )
                .mappings()
                .first()
            )
            if (
                not provider
                or not credential
                or not provider["config"]["enabled"]
                or not credential["enabled"]
            ):
                raise GatewayError("upstream_unavailable", 503)
            now = await session.scalar(text("SELECT clock_timestamp()"))
            until = max((record["cooldown_until"] for record in (provider, credential)
                         if record["cooldown_until"] is not None), default=now)
            if until > now:
                raise GatewayError("rate_limited", 429, "upstream",
                                   min(86400, ceil((until - now).total_seconds())))
            limits = []
            for column, record_id, rpm, concurrency in (
                (
                    "provider_id",
                    route.provider_id,
                    provider["config"].get("rpm_limit", 600),
                    provider["config"].get("concurrency_limit", 16),
                ),
                (
                    "credential_id",
                    route.credential_id,
                    credential["rpm_limit"],
                    credential["concurrency_limit"],
                ),
            ):
                counts = (
                    (
                        await session.execute(
                            text(
                                f"SELECT count(*) FILTER(WHERE admitted_at>now()-interval '60 seconds') AS rpm, "
                                f"count(*) FILTER(WHERE active AND deadline>now()) AS concurrent FROM provider_admissions WHERE {column}=:id"
                            ),
                            {"id": record_id},
                        )
                    )
                    .mappings()
                    .one()
                )
                limits.append((counts, rpm, concurrency))
            if any(counts["rpm"] >= rpm for counts, rpm, _ in limits):
                raise GatewayError("upstream_unavailable", 503, "upstream", 1)
            if any(counts["concurrent"] >= concurrency for counts, _, concurrency in limits):
                return False
            inserted = await session.execute(
                text(
                    "INSERT INTO provider_admissions(id,provider_id,credential_id,deadline) "
                    "SELECT :id,:provider,:credential,:deadline "
                    "WHERE clock_timestamp() < :deadline RETURNING id"
                ),
                {
                    "id": identity,
                    "provider": route.provider_id,
                    "credential": route.credential_id,
                    "deadline": deadline,
                },
            )
            if inserted.first() is None:
                raise GatewayError("deadline_exceeded", 504, "upstream")
        return True

    def _remaining(self, deadline):
        now = datetime.now(deadline.tzinfo or timezone.utc)
        return (deadline - now).total_seconds()

    def snapshot(self):
        return {
            "wait_attempts": self._wait_attempts,
            "expired_waits": self._expired_waits,
        }

    async def _wait_for_release(self, deadline):
        remaining = self._remaining(deadline)
        if remaining <= 0:
            self._expired_waits += 1
            raise GatewayError("deadline_exceeded", 504, "upstream")
        async with self._released:
            try:
                await asyncio.wait_for(self._released.wait(), min(0.05, remaining))
            except asyncio.TimeoutError:
                pass

    async def _notify_release(self):
        async with self._released:
            self._released.notify_all()

    @asynccontextmanager
    async def acquire(self, route, deadline, *, wait=False):
        identity = uuid4()
        while True:
            if wait and self._remaining(deadline) <= 0:
                self._expired_waits += 1
                raise GatewayError("deadline_exceeded", 504, "upstream")
            try:
                acquired = await self._try_acquire(route, deadline, identity)
            except GatewayError as exc:
                if wait and exc.code == "deadline_exceeded":
                    self._expired_waits += 1
                raise
            if acquired:
                break
            if not wait:
                raise GatewayError("upstream_unavailable", 503, "upstream", 1)
            self._wait_attempts += 1
            await self._wait_for_release(deadline)
        try:
            yield identity
        finally:
            try:
                async with self.db.sessions.begin() as session:
                    await session.execute(
                        text("UPDATE provider_admissions SET active=false WHERE id=:id"), {"id": identity}
                    )
            finally:
                await self._notify_release()
