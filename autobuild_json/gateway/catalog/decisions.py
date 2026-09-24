"""Persistent review decisions for catalog entries."""

from uuid import UUID, uuid4

from sqlalchemy import text

from ..errors import GatewayError
from .records import DecisionEdit


class DecisionStore:
    def __init__(self, db, sources):
        self.db, self.sources = db, sources

    async def apply(self, source_id: UUID, expected_version: int, edits: tuple[DecisionEdit, ...], actor) -> list[dict]:
        if type(expected_version) is not int or expected_version < 1 or not isinstance(edits, tuple):
            raise GatewayError("invalid_request")
        if len({edit.upstream_id for edit in edits}) != len(edits):
            raise GatewayError("invalid_request")
        async with self.db.sessions.begin() as session:
            source = (await session.execute(text("SELECT * FROM catalog_sources WHERE id=:id FOR UPDATE"), {"id": source_id})).mappings().first()
            if source is None:
                raise GatewayError("not_found", 404)
            if source["version"] != expected_version:
                raise GatewayError("version_conflict", 409)
            result = []
            for edit in sorted(edits, key=lambda item: item.upstream_id):
                row = (await session.execute(text("SELECT * FROM catalog_decisions WHERE source_id=:source AND upstream_id=:upstream FOR UPDATE"),
                                             {"source": source_id, "upstream": edit.upstream_id})).mappings().first()
                if row is None:
                    if edit.expected_version is not None:
                        raise GatewayError("version_conflict", 409)
                    row = (await session.execute(text("""INSERT INTO catalog_decisions(source_id,upstream_id,ignored)
                        VALUES (:source,:upstream,:ignored) RETURNING *"""),
                        {"source": source_id, "upstream": edit.upstream_id, "ignored": edit.ignored})).mappings().one()
                else:
                    if edit.expected_version is None or row["version"] != edit.expected_version:
                        raise GatewayError("version_conflict", 409)
                    row = (await session.execute(text("""UPDATE catalog_decisions SET ignored=:ignored,
                        version=version+1,updated_at=clock_timestamp()
                        WHERE source_id=:source AND upstream_id=:upstream AND version=:version RETURNING *"""),
                        {"source": source_id, "upstream": edit.upstream_id, "ignored": edit.ignored,
                         "version": edit.expected_version})).mappings().one()
                await session.execute(text("""INSERT INTO audit_events(id,actor,action,record_id)
                    VALUES (:id,:actor,:action,:record)"""),
                    {"id": uuid4(), "actor": str(actor), "action": "catalog.decision.updated", "record": source_id})
                result.append({"source_id": str(source_id), "upstream_id": row["upstream_id"],
                               "version": row["version"], "ignored": row["ignored"],
                               "binding_id": str(row["binding_id"]) if row["binding_id"] else None,
                               "published_run_id": str(row["published_run_id"]) if row["published_run_id"] else None})
            return result
