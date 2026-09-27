import asyncio
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from math import ceil
import time
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from ..errors import GatewayError


class ProviderLimits:
    _CLEANUP_GRACE_SECONDS = 0.25

    def __init__(self, db):
        self.db = db
        self._released = asyncio.Condition()
        self._wait_attempts = 0
        self._expired_waits = 0
        self._waits = deque(maxlen=2048)
        self._last_wait_ms = 0.0

    async def _set_lock_timeout(self, session, deadline):
        remaining = self._remaining(deadline)
        if remaining <= 0:
            raise GatewayError("deadline_exceeded", 504, "upstream")
        await session.execute(
            text("SELECT set_config('lock_timeout', :timeout, true)"),
            {"timeout": f"{max(1, int(remaining * 1000))}ms"},
        )

    async def _try_acquire(self, route, deadline, identity):
        try:
            async with self.db.sessions.begin() as session:
                await self._set_lock_timeout(session, deadline)
                provider = (await session.execute(
                    text("SELECT config,version,cooldown_until FROM providers WHERE id=:id FOR UPDATE"),
                    {"id": route.provider_id})).mappings().first()
                await self._set_lock_timeout(session, deadline)
                credential = (await session.execute(
                    text("SELECT * FROM credentials WHERE id=:id AND provider_id=:provider FOR UPDATE"),
                    {"id": route.credential_id, "provider": route.provider_id})).mappings().first()
                if (not provider or not credential or not provider["config"]["enabled"]
                        or not credential["enabled"]):
                    raise GatewayError("upstream_unavailable", 503)
                now = await session.scalar(text("SELECT clock_timestamp()"))
                until = max((record["cooldown_until"] for record in (provider, credential)
                             if record["cooldown_until"] is not None), default=now)
                if until > now:
                    raise GatewayError("rate_limited", 429, "upstream",
                                       min(86400, ceil((until - now).total_seconds())))
                limits = []
                for column, record_id, rpm, concurrency in (
                    ("provider_id", route.provider_id, provider["config"].get("rpm_limit", 600),
                     provider["config"].get("concurrency_limit", 16)),
                    ("credential_id", route.credential_id, credential["rpm_limit"],
                     credential["concurrency_limit"]),
                ):
                    counts = (await session.execute(text(
                        f"SELECT count(*) FILTER(WHERE admitted_at>now()-interval '60 seconds') AS rpm, "
                        f"count(*) FILTER(WHERE active AND deadline>now()) AS concurrent "
                        f"FROM provider_admissions WHERE {column}=:id"), {"id": record_id})).mappings().one()
                    limits.append((counts, rpm, concurrency))
                if any(counts["rpm"] >= rpm for counts, rpm, _ in limits):
                    raise GatewayError("upstream_unavailable", 503, "upstream", 1)
                if any(counts["concurrent"] >= concurrency for counts, _, concurrency in limits):
                    return False
                await self._set_lock_timeout(session, deadline)
                inserted = await session.execute(text(
                    "INSERT INTO provider_admissions(id,provider_id,credential_id,deadline) "
                    "SELECT :id,:provider,:credential,:deadline "
                    "WHERE clock_timestamp() < :deadline RETURNING id"), {
                        "id": identity, "provider": route.provider_id,
                        "credential": route.credential_id, "deadline": deadline})
                if inserted.first() is None:
                    raise GatewayError("deadline_exceeded", 504, "upstream")
            return True
        except (OperationalError, DBAPIError) as exc:
            original = getattr(exc, "orig", None)
            pgcode = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
            message = str(original or exc).lower()
            if pgcode == "55P03" or "lock timeout" in message:
                raise GatewayError("deadline_exceeded", 504, "upstream") from None
            raise

    def _remaining(self, deadline):
        now = datetime.now(deadline.tzinfo or timezone.utc)
        return (deadline - now).total_seconds()

    def snapshot(self):
        waits = sorted(self._waits)

        def percentile(percent):
            if not waits:
                return 0.0
            return waits[max(0, ceil(len(waits) * percent) - 1)]

        return {
            "wait_attempts": self._wait_attempts,
            "expired_waits": self._expired_waits,
            "wait_p50_ms": percentile(.50),
            "wait_p95_ms": percentile(.95),
            "last_wait_ms": self._last_wait_ms,
        }

    async def _wait_for_release(self, deadline):
        started = time.monotonic()
        remaining = self._remaining(deadline)
        if remaining <= 0:
            self._expired_waits += 1
            raise GatewayError("deadline_exceeded", 504, "upstream")
        try:
            async with self._released:
                try:
                    await asyncio.wait_for(self._released.wait(), min(0.05, remaining))
                except asyncio.TimeoutError:
                    pass
        finally:
            wait_ms = min((time.monotonic() - started) * 1000, self._CLEANUP_GRACE_SECONDS * 1000)
            self._waits.append(wait_ms)
            self._last_wait_ms = wait_ms

    async def _notify_release(self):
        async with self._released:
            self._released.notify_all()

    async def _release_admission(self, identity):
        cleanup_deadline = datetime.now(timezone.utc) + timedelta(seconds=self._CLEANUP_GRACE_SECONDS)
        try:
            async with self.db.sessions.begin() as session:
                await self._set_lock_timeout(session, cleanup_deadline)
                await session.execute(
                    text("UPDATE provider_admissions SET active=false WHERE id=:id"), {"id": identity}
                )
        except (OperationalError, DBAPIError) as exc:
            original = getattr(exc, "orig", None)
            pgcode = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
            message = str(original or exc).lower()
            if pgcode == "55P03" or "lock timeout" in message:
                raise GatewayError("deadline_exceeded", 504, "upstream") from None
            raise

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
                owned = asyncio.create_task(self._release_admission(identity))
                cancelled = False
                while not owned.done():
                    try:
                        await asyncio.shield(owned)
                    except asyncio.CancelledError:
                        cancelled = True
                await owned
                if cancelled:
                    raise asyncio.CancelledError
            finally:
                await self._notify_release()
