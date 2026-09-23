from sqlalchemy import text


class UsageReports:
    def __init__(self, db):
        self.db = db

    async def requests(self, limit=100):
        async with self.db.sessions() as session:
            return [dict(row) for row in (await session.execute(text("SELECT id,key_id,model_id,protocol,state,admitted_at,"
                "hold,usage,reason FROM requests ORDER BY admitted_at DESC LIMIT :limit"), {"limit": min(500, max(1, limit))})).mappings()]

    async def overview(self):
        async with self.db.sessions() as session:
            return dict((await session.execute(text("SELECT count(*) AS requests, "
                "count(*) FILTER (WHERE state='usage_pending') AS pending, "
                "count(*) FILTER (WHERE state='completed') AS completed FROM requests"))).mappings().one())

    async def audit(self):
        async with self.db.sessions() as session:
            return [dict(row) for row in (await session.execute(text("SELECT id,actor,action,record_id,details,created_at "
                "FROM audit_events ORDER BY created_at DESC LIMIT 200"))).mappings()]
