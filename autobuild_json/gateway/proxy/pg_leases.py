from datetime import timedelta, timezone

from sqlalchemy import text

from .leases import LeaseToken


class PgLeaseStore:
    def __init__(self, db, *, clock=None):
        self.db, self.clock = db, clock

    async def _now(self, session):
        return (self.clock() if self.clock else await session.scalar(text("SELECT clock_timestamp()"))).astimezone(timezone.utc)

    async def disable_resource(self, resource):
        async with self.db.sessions.begin() as session:
            await session.execute(text("INSERT INTO proxy_health(resource,disabled) VALUES (:resource,true) "
                                       "ON CONFLICT(resource) DO UPDATE SET disabled=true"), {"resource": resource})

    async def is_disabled(self, resource):
        async with self.db.sessions() as session:
            return bool(await session.scalar(text("SELECT disabled FROM proxy_health WHERE resource=:resource"),
                                             {"resource": resource}))

    async def claim(self, resource, owner, deadline):
        async with self.db.sessions.begin() as session:
            now = await self._now(session)
            if deadline <= now or deadline > now+timedelta(seconds=600):
                raise ValueError("invalid_deadline")
            await session.execute(text("INSERT INTO proxy_leases(resource,generation) VALUES (:resource,0) "
                                       "ON CONFLICT DO NOTHING"), {"resource": resource})
            row = (await session.execute(text("SELECT * FROM proxy_leases WHERE resource=:resource FOR UPDATE"),
                                         {"resource": resource})).mappings().one()
            if row["owner"] is not None and row["hard_deadline"] + timedelta(seconds=15) > now:
                return None
            generation = row["generation"] + 1
            await session.execute(text("UPDATE proxy_leases SET owner=:owner,generation=:generation,hard_deadline=:deadline "
                                       "WHERE resource=:resource"),
                                  {"owner": owner, "generation": generation, "deadline": deadline, "resource": resource})
            return LeaseToken(resource, owner, generation, deadline)

    async def release(self, token):
        async with self.db.sessions.begin() as session:
            await session.execute(text("UPDATE proxy_leases SET owner=NULL WHERE resource=:resource "
                "AND owner=:owner AND generation=:generation"),
                {"resource": token.resource, "owner": token.owner, "generation": token.generation})

    async def assert_owner(self, token):
        async with self.db.sessions() as session:
            now = await self._now(session)
            return bool(await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE resource=:resource "
                "AND owner=:owner AND generation=:generation AND hard_deadline>:now"),
                {"resource": token.resource, "owner": token.owner, "generation": token.generation, "now": now}))

    async def renew(self, token):
        return await self.assert_owner(token)
