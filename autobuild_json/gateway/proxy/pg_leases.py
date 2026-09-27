import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from ..errors import GatewayError
from .leases import LeaseToken


class PgLeaseStore:
    _CLEANUP_GRACE_SECONDS = 0.25

    def __init__(self, db, *, clock=None):
        self.db, self.clock = db, clock

    async def _now(self, session):
        return (self.clock() if self.clock else await session.scalar(text("SELECT clock_timestamp()"))).astimezone(timezone.utc)

    def _remaining(self, deadline):
        return (deadline - datetime.now(deadline.tzinfo or timezone.utc)).total_seconds()

    async def _set_lock_timeout(self, session, deadline):
        remaining = self._remaining(deadline)
        if remaining <= 0:
            raise GatewayError("deadline_exceeded", 504, "proxy")
        await session.execute(
            text("SELECT set_config('lock_timeout', :timeout, true)"),
            {"timeout": f"{max(1, int(remaining * 1000))}ms"},
        )

    async def disable_resource(self, resource):
        async with self.db.sessions.begin() as session:
            await session.execute(text("INSERT INTO proxy_health(resource,disabled) VALUES (:resource,true) "
                                       "ON CONFLICT(resource) DO UPDATE SET disabled=true"), {"resource": resource})

    async def is_disabled(self, resource):
        async with self.db.sessions() as session:
            return bool(await session.scalar(text("SELECT disabled FROM proxy_health WHERE resource=:resource"),
                                             {"resource": resource}))

    async def _claim_transaction(self, resource, owner, deadline):
        async with self.db.sessions.begin() as session:
            now = await self._now(session)
            if deadline <= now or deadline > now+timedelta(seconds=600):
                raise GatewayError("deadline_exceeded", 504, "proxy")
            await self._set_lock_timeout(session, deadline)
            await session.execute(text("INSERT INTO proxy_leases(resource,generation) VALUES (:resource,0) "
                                       "ON CONFLICT DO NOTHING"), {"resource": resource})
            await self._set_lock_timeout(session, deadline)
            row = (await session.execute(text(
                "SELECT * FROM proxy_leases WHERE resource=:resource FOR UPDATE"),
                {"resource": resource})).mappings().one()
            now = await self._now(session)
            if deadline <= now:
                raise GatewayError("deadline_exceeded", 504, "proxy")
            if row["owner"] is not None and row["hard_deadline"] + timedelta(seconds=15) > now:
                return None
            generation = row["generation"] + 1
            await self._set_lock_timeout(session, deadline)
            updated = await session.execute(text(
                "UPDATE proxy_leases SET owner=:owner,generation=:generation,hard_deadline=:deadline "
                "WHERE resource=:resource AND clock_timestamp()<:deadline RETURNING generation"),
                {"owner": owner, "generation": generation, "deadline": deadline, "resource": resource})
            if updated.first() is None or self._remaining(deadline) <= 0:
                raise GatewayError("deadline_exceeded", 504, "proxy")
            return LeaseToken(resource, owner, generation, deadline)

    async def claim(self, resource, owner, deadline):
        try:
            remaining = self._remaining(deadline)
            if remaining <= 0 or remaining > 600:
                raise GatewayError("deadline_exceeded", 504, "proxy")
            return await asyncio.wait_for(self._claim_transaction(resource, owner, deadline), remaining)
        except asyncio.TimeoutError:
            raise GatewayError("deadline_exceeded", 504, "proxy") from None
        except (OperationalError, DBAPIError) as exc:
            original = getattr(exc, "orig", None)
            pgcode = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
            message = str(original or exc).lower()
            if pgcode == "55P03" or "lock timeout" in message:
                raise GatewayError("deadline_exceeded", 504, "proxy") from None
            raise

    async def _release_transaction(self, token, cleanup_deadline):
        async with self.db.sessions.begin() as session:
            await self._set_lock_timeout(session, cleanup_deadline)
            await session.execute(text("UPDATE proxy_leases SET owner=NULL WHERE resource=:resource "
                "AND owner=:owner AND generation=:generation"),
                {"resource": token.resource, "owner": token.owner, "generation": token.generation})

    async def _release(self, token):
        cleanup_deadline = datetime.now(timezone.utc) + timedelta(seconds=self._CLEANUP_GRACE_SECONDS)
        try:
            await asyncio.wait_for(self._release_transaction(token, cleanup_deadline), self._CLEANUP_GRACE_SECONDS)
        except asyncio.TimeoutError:
            raise GatewayError("deadline_exceeded", 504, "proxy") from None
        except (OperationalError, DBAPIError) as exc:
            original = getattr(exc, "orig", None)
            pgcode = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
            message = str(original or exc).lower()
            if pgcode == "55P03" or "lock timeout" in message:
                raise GatewayError("deadline_exceeded", 504, "proxy") from None
            raise

    async def release(self, token):
        owned = asyncio.create_task(self._release(token))
        cancelled = False
        cleanup_error = None
        while not owned.done():
            try:
                await asyncio.shield(owned)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                # The owned task has completed with its cleanup result; the
                # final await below records the error without abandoning it.
                break
        try:
            await owned
        except BaseException as exc:
            cleanup_error = exc
        if cancelled:
            raise asyncio.CancelledError from cleanup_error
        if cleanup_error is not None:
            raise cleanup_error

    async def assert_owner(self, token):
        async with self.db.sessions() as session:
            now = await self._now(session)
            return bool(await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE resource=:resource "
                "AND owner=:owner AND generation=:generation AND hard_deadline>:now"),
                {"resource": token.resource, "owner": token.owner, "generation": token.generation, "now": now}))

    async def renew(self, token):
        return await self.assert_owner(token)
