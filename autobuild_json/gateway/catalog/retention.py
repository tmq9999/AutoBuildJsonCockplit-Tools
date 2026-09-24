"""Bounded deletion of unreferenced snapshots; never truncate accounting."""

from sqlalchemy import text


_RANKED = """WITH ranked AS (
    SELECT o.*,row_number() OVER (PARTITION BY source_id ORDER BY created_at DESC,id DESC) AS rank
    FROM provider_operations o WHERE kind='discover' AND state='succeeded'
), candidates AS (
    SELECT o.id,o.source_id,o.created_at,
      (o.retention_protected OR o.budget_id IS NOT NULL OR o.upper_cost IS NOT NULL OR o.usage IS NOT NULL
       OR o.cost IS NOT NULL OR EXISTS(SELECT 1 FROM catalog_sources s WHERE
           s.latest_successful_run_id=o.id OR s.previous_successful_run_id=o.id)
       OR EXISTS(SELECT 1 FROM catalog_decisions d WHERE d.published_run_id=o.id)
       OR EXISTS(SELECT 1 FROM catalog_publications p WHERE p.run_id=o.id)) AS protected
    FROM ranked o WHERE rank>20 OR created_at<:now-interval '30 days'
    UNION ALL
    SELECT id,source_id,created_at,true FROM provider_operations
      WHERE kind='probe' AND created_at<:now-interval '30 days'
) """


class Retention:
    def __init__(self, db):
        self.db = db

    async def protected_count(self, source_id) -> int:
        """Read-only status for the requested source, independent of purge batches."""
        from datetime import datetime, timezone
        async with self.db.sessions() as session:
            return await session.scalar(text(_RANKED + "SELECT count(*) FROM candidates WHERE protected AND source_id=:source"),
                                        {"now": datetime.now(timezone.utc), "source": source_id})

    async def purge(self, now, limit=100) -> dict:
        async with self.db.sessions.begin() as session:
            # Serialize with source snapshot/publication writers, before operation
            # locks. Deletion never asks for provider locks, avoiding a reverse edge.
            # Only sources that can actually yield an unprotected row consume
            # the bounded deletion-source budget. Protected-only sources are
            # intentionally excluded here so low UUIDs cannot starve purgeable
            # sources forever; their protected rows remain untouched.
            sources = (await session.execute(text(_RANKED + "SELECT DISTINCT source_id FROM candidates "
                "WHERE NOT protected ORDER BY source_id LIMIT :limit"),
                {"now": now, "limit": max(0, min(limit, 100))})).scalars().all()
            deleted = 0
            # Status sampling has a separate bounded budget. A protected-only
            # source is still reported without consuming a deletion slot.
            protected_rows = (await session.execute(text(_RANKED +
                "SELECT source_id,count(*) AS count FROM candidates WHERE protected "
                "GROUP BY source_id ORDER BY source_id LIMIT 100"), {"now": now})).mappings().all()
            protected = {str(row["source_id"]): row["count"] for row in protected_rows}
            for source_id in sources:
                locked = await session.scalar(text("SELECT id FROM catalog_sources WHERE id=:id FOR UPDATE SKIP LOCKED"),
                                              {"id": source_id})
                if locked is None:
                    continue
                ids = (await session.execute(text(_RANKED + "SELECT id FROM candidates WHERE source_id=:source "
                    "AND NOT protected ORDER BY created_at,id LIMIT :limit"),
                    {"now": now, "source": source_id, "limit": max(0, min(limit, 100)-deleted)})).scalars().all()
                if ids:
                    # Entry and client-claim foreign keys cascade in this transaction.
                    result = await session.execute(text("DELETE FROM provider_operations WHERE id=ANY(:ids)"), {"ids": ids})
                    deleted += result.rowcount
            return {"deleted": deleted, "protected_over_limit": protected}
