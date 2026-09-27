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


class _ProviderLockEntry:
    __slots__ = ("lock", "users")

    def __init__(self):
        self.lock = asyncio.Lock()
        self.users = 0


class ProviderLimits:
    _PROVIDER_LOCK_CAPACITY = 256
    _CLEANUP_GRACE_SECONDS = 0.25
    _CLEANUP_RETRY_SECONDS = 0.5
    _CLEANUP_RETRY_ATTEMPTS = 2

    def __init__(self, db):
        self.db = db
        self._released = asyncio.Condition()
        self._wait_attempts = 0
        self._expired_waits = 0
        self._cancelled_waits = 0
        self._waits = deque(maxlen=2048)
        self._last_wait_ms = 0.0
        self._cleanup_failures = 0
        self._provider_locks = {}
        self._provider_lock_overflow = tuple(_ProviderLockEntry() for _ in range(32))
        self._provider_overflow_keys = {}

    def _provider_lock(self, provider_id):
        overflow = self._provider_overflow_keys.get(provider_id)
        if overflow is not None:
            overflow.users += 1
            return overflow
        entry = self._provider_locks.get(provider_id)
        if entry is not None:
            entry.users += 1
            return entry
        if len(self._provider_locks) >= self._PROVIDER_LOCK_CAPACITY:
            for key, candidate in tuple(self._provider_locks.items()):
                if (candidate.users == 0 and not candidate.lock.locked()
                        and not getattr(candidate.lock, "_waiters", None)):
                    del self._provider_locks[key]
                    break
        if len(self._provider_locks) >= self._PROVIDER_LOCK_CAPACITY:
            entry = self._provider_overflow_keys.get(provider_id)
            if entry is None:
                entry = self._provider_lock_overflow[hash(str(provider_id)) % len(self._provider_lock_overflow)]
                if len(self._provider_overflow_keys) < self._PROVIDER_LOCK_CAPACITY:
                    self._provider_overflow_keys[provider_id] = entry
        else:
            entry = _ProviderLockEntry()
            self._provider_locks[provider_id] = entry
        entry.users += 1
        return entry

    def _provider_unlock(self, provider_id, entry):
        entry.users -= 1
        if entry.users == 0 and self._provider_overflow_keys.get(provider_id) is entry:
            del self._provider_overflow_keys[provider_id]

    async def _try_acquire_bounded(self, route, deadline, identity):
        remaining = self._remaining(deadline)
        if remaining <= 0:
            raise GatewayError("deadline_exceeded", 504, "upstream")
        provider_entry = self._provider_lock(route.provider_id)
        acquired = False
        try:
            try:
                await asyncio.wait_for(provider_entry.lock.acquire(), remaining)
                acquired = True
            except asyncio.TimeoutError:
                raise GatewayError("deadline_exceeded", 504, "upstream") from None
            remaining = self._remaining(deadline)
            if remaining <= 0:
                raise GatewayError("deadline_exceeded", 504, "upstream")
            return await asyncio.wait_for(self._try_acquire(route, deadline, identity), remaining)
        except asyncio.TimeoutError:
            raise GatewayError("deadline_exceeded", 504, "upstream") from None
        finally:
            if acquired:
                provider_entry.lock.release()
            self._provider_unlock(route.provider_id, provider_entry)

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
            "cancelled_waits": self._cancelled_waits,
            "wait_p50_ms": percentile(.50),
            "wait_p95_ms": percentile(.95),
            "last_wait_ms": self._last_wait_ms,
            "cleanup_failures": self._cleanup_failures,
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

    async def _release_admission(self, identity):
        cleanup_deadline = datetime.now(timezone.utc) + timedelta(seconds=self._CLEANUP_GRACE_SECONDS)
        async def operation():
            async with self.db.sessions.begin() as session:
                await self._set_lock_timeout(session, cleanup_deadline)
                await session.execute(
                    text("UPDATE provider_admissions SET active=false WHERE id=:id"), {"id": identity}
                )
        try:
            await asyncio.wait_for(operation(), self._CLEANUP_GRACE_SECONDS)
        except asyncio.TimeoutError:
            raise GatewayError("deadline_exceeded", 504, "upstream") from None
        except (OperationalError, DBAPIError) as exc:
            original = getattr(exc, "orig", None)
            pgcode = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
            message = str(original or exc).lower()
            if pgcode == "55P03" or "lock timeout" in message:
                raise GatewayError("deadline_exceeded", 504, "upstream") from None
            raise

    async def _cleanup_with_retry(self, identity, *, seconds, attempts, raise_failure):
        """Retry a transient pool/lock failure without extending request work."""
        end = time.monotonic() + seconds
        failure = None
        for _ in range(attempts):
            if time.monotonic() >= end:
                break
            try:
                await self._release_admission(identity)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = exc
                await asyncio.sleep(0)
        if failure is not None:
            self._cleanup_failures += 1
            if raise_failure:
                raise failure

    @asynccontextmanager
    async def acquire(self, route, deadline, *, wait=False):
        identity = uuid4()
        wait_started = None
        try:
            while True:
                if wait and self._remaining(deadline) <= 0:
                    self._expired_waits += 1
                    raise GatewayError("deadline_exceeded", 504, "upstream")
                try:
                    acquired = await self._try_acquire_bounded(route, deadline, identity)
                except GatewayError as exc:
                    if wait and exc.code == "deadline_exceeded":
                        self._expired_waits += 1
                    raise
                if acquired:
                    break
                if not wait:
                    raise GatewayError("upstream_unavailable", 503, "upstream", 1)
                if wait_started is None:
                    wait_started = time.monotonic()
                self._wait_attempts += 1
                await self._wait_for_release(deadline)
        except asyncio.CancelledError:
            if wait and wait_started is not None:
                self._cancelled_waits += 1
            raise
        finally:
            if wait_started is not None:
                wait_ms = (time.monotonic() - wait_started) * 1000
                self._waits.append(wait_ms)
                self._last_wait_ms = wait_ms
        try:
            yield identity
        finally:
            try:
                # The response/ledger result is already authoritative here.
                # Join bounded recovery, but never replace that result with a
                # transient pool cleanup timeout; the failure is counted.
                owned = asyncio.create_task(self._cleanup_with_retry(
                    identity,
                    seconds=self._CLEANUP_RETRY_SECONDS,
                    attempts=self._CLEANUP_RETRY_ATTEMPTS,
                    raise_failure=False,
                ))
                cancelled = False
                cleanup_error = None
                while not owned.done():
                    try:
                        await asyncio.shield(owned)
                    except asyncio.CancelledError:
                        cancelled = True
                try:
                    await owned
                except BaseException as exc:
                    cleanup_error = exc
                if cancelled:
                    raise asyncio.CancelledError from cleanup_error
                if cleanup_error is not None:
                    raise cleanup_error
            finally:
                await self._notify_release()
