"""Atomic immutable catalog snapshot commits and stable snapshot paging."""

from datetime import datetime, timezone
import json
from uuid import UUID

from sqlalchemy import text

from ..errors import GatewayError
from .cursors import decode_cursor, encode_cursor
from .digests import content_digest
from .records import CatalogEntry, SnapshotItem, SnapshotPage

_DIFFS = frozenset({"added", "changed", "missing", "unchanged"})


def _metadata(entry: CatalogEntry) -> str:
    return json.dumps(entry.model_dump(mode="json")["metadata"], ensure_ascii=False, separators=(",", ":"))


def _entry(row) -> CatalogEntry:
    return CatalogEntry(upstream_id=row["upstream_id"], metadata=row["metadata"] or {})


def _same_stamp(actual, expected) -> bool:
    return all(getattr(actual, name) == getattr(expected, name) for name in
               ("source_version", "provider_version", "credential_version", "proxy_id", "proxy_version", "content_digest"))


class SnapshotStore:
    def __init__(self, db, sources, ops, cursor_key: bytes):
        self.db, self.sources, self.ops, self.cursor_key = db, sources, ops, cursor_key

    async def commit(self, claim, context, entries: tuple[CatalogEntry, ...]) -> UUID:
        if not isinstance(entries, tuple):
            raise GatewayError("invalid_request")
        normalized = tuple(CatalogEntry.model_validate(entry.model_dump(mode="json")) for entry in entries)
        ids = [entry.upstream_id for entry in normalized]
        if len(set(ids)) != len(ids):
            raise GatewayError("invalid_request")
        async with self.db.sessions.begin() as session:
            locked_context = await self.sources.lock_context(session, context.source.id)
            if locked_context is None or claim.source_id != context.source.id or claim.source_id != locked_context.source.id:
                raise GatewayError("version_conflict", 409)
            if not _same_stamp(locked_context.stamp, context.stamp):
                raise GatewayError("version_conflict", 409)
            operation = await self.ops.assert_claim(session, claim)
            if operation.source_id != locked_context.source.id:
                raise GatewayError("version_conflict", 409)
            expected_row = (await session.execute(text("SELECT expected FROM provider_operations WHERE id=:id FOR UPDATE"),
                                                   {"id": claim.operation_id})).mappings().one()
            from .records import ConfigStamp
            expected = ConfigStamp.model_validate(expected_row["expected"])
            if not _same_stamp(locked_context.stamp, expected):
                raise GatewayError("version_conflict", 409)

            source_row = (await session.execute(text("SELECT * FROM catalog_sources WHERE id=:id FOR UPDATE"),
                                                {"id": context.source.id})).mappings().one()
            previous_id = source_row["latest_successful_run_id"]
            previous_rows = {}
            previous_content_digest = None
            if previous_id is not None:
                previous_operation = (await session.execute(text("SELECT source_id,state FROM provider_operations WHERE id=:id"),
                                                             {"id": previous_id})).mappings().first()
                if previous_operation is None or previous_operation["source_id"] != context.source.id or previous_operation["state"] != "succeeded":
                    raise GatewayError("catalog_snapshot_stale", 409)
                previous_rows = {row["upstream_id"]: row for row in
                    (await session.execute(text("SELECT upstream_id,metadata,digest FROM catalog_entries WHERE run_id=:run"),
                                            {"run": previous_id})).mappings().all()}
                previous_expected = await session.scalar(text("SELECT expected FROM provider_operations WHERE id=:id"), {"id": previous_id})
                previous_content_digest = previous_expected.get("content_digest") if isinstance(previous_expected, dict) else None
            source_changed = previous_id is not None and previous_content_digest != locked_context.stamp.content_digest
            incoming_digest = {entry.upstream_id: bytes.fromhex(content_digest((entry,))) for entry in normalized}
            previous_digest = {key: bytes(row["digest"]) for key, row in previous_rows.items()}
            all_ids = set(incoming_digest) | set(previous_digest)
            counts = {name: 0 for name in _DIFFS}
            for upstream_id in all_ids:
                if upstream_id not in previous_digest:
                    counts["added"] += 1
                elif upstream_id not in incoming_digest and not source_changed:
                    counts["missing"] += 1
                elif upstream_id not in incoming_digest:
                    continue
                elif incoming_digest[upstream_id] == previous_digest[upstream_id]:
                    counts["unchanged"] += 1
                else:
                    counts["changed"] += 1
            for entry in normalized:
                await session.execute(text("""INSERT INTO catalog_entries(run_id,upstream_id,metadata,digest)
                    VALUES (:run,:id,CAST(:metadata AS jsonb),:digest)"""),
                    {"run": claim.operation_id, "id": entry.upstream_id, "metadata": _metadata(entry),
                     "digest": incoming_digest[entry.upstream_id]})
            result = {"run_id": str(claim.operation_id), "count": len(normalized), **counts}
            updated = (await session.execute(text("""UPDATE provider_operations SET state='succeeded',
                result=CAST(:result AS jsonb),finished_at=clock_timestamp(),version=version+1
                WHERE id=:id AND owner=:owner AND generation=:generation AND state='running'
                  AND deadline>clock_timestamp() AND NOT cancel_requested RETURNING id"""),
                {"id": claim.operation_id, "owner": claim.owner, "generation": claim.generation,
                 "result": json.dumps(result, separators=(",", ":"))})).first()
            if updated is None:
                raise GatewayError("claim_lost", 409)
            await session.execute(text("""UPDATE catalog_sources SET latest_successful_run_id=:latest,
                previous_successful_run_id=:previous,updated_at=clock_timestamp()
                WHERE id=:source"""), {"latest": claim.operation_id, "previous": previous_id, "source": context.source.id})
            return claim.operation_id

    async def page(self, source_id: UUID, *, cursor=None, limit=50, diff=None) -> SnapshotPage:
        if type(limit) is not int or not 1 <= limit <= 200 or (diff is not None and diff not in _DIFFS):
            raise GatewayError("invalid_request")
        async with self.db.sessions() as session:
            source = (await session.execute(text("SELECT * FROM catalog_sources WHERE id=:id"), {"id": source_id})).mappings().first()
            if source is None:
                raise GatewayError("not_found", 404)
            if cursor is not None:
                payload = decode_cursor(cursor, self.cursor_key, datetime.now(timezone.utc))
                if payload["source"] != str(source_id) or payload["filter"] != diff:
                    raise GatewayError("invalid_request")
                run_id, previous_id, after = UUID(payload["run"]), (UUID(payload["previous"]) if payload["previous"] else None), payload["after"]
            else:
                run_id, previous_id, after = source["latest_successful_run_id"], source["previous_successful_run_id"], None
            if run_id is None:
                return SnapshotPage(run_id=UUID(int=0), previous_run_id=None, source_changed=False, items=(), next_cursor=None)
            run = (await session.execute(text("SELECT source_id,state FROM provider_operations WHERE id=:id"), {"id": run_id})).mappings().first()
            if run is None or run["source_id"] != source_id or run["state"] != "succeeded":
                raise GatewayError("catalog_snapshot_stale", 409)
            current_rows = {row["upstream_id"]: row for row in
                            (await session.execute(text("SELECT upstream_id,metadata,digest FROM catalog_entries WHERE run_id=:run"), {"run": run_id})).mappings().all()}
            previous_rows = {}
            previous_content_digest = None
            if previous_id is not None:
                previous_run = (await session.execute(text("SELECT source_id,state FROM provider_operations WHERE id=:id"), {"id": previous_id})).mappings().first()
                if previous_run is None or previous_run["source_id"] != source_id or previous_run["state"] != "succeeded":
                    raise GatewayError("catalog_snapshot_stale", 409)
                previous_rows = {row["upstream_id"]: row for row in
                                 (await session.execute(text("SELECT upstream_id,metadata,digest FROM catalog_entries WHERE run_id=:run"), {"run": previous_id})).mappings().all()}
                previous_expected = await session.scalar(text("SELECT expected FROM provider_operations WHERE id=:id"), {"id": previous_id})
                previous_content_digest = previous_expected.get("content_digest") if isinstance(previous_expected, dict) else None
            current_expected = await session.scalar(text("SELECT expected FROM provider_operations WHERE id=:id"), {"id": run_id})
            source_changed = previous_id is not None and isinstance(current_expected, dict) and previous_content_digest != current_expected.get("content_digest")
            ids = sorted(set(current_rows) | set(previous_rows))
            if after is not None:
                ids = [item for item in ids if item > after]
            rows = []
            for upstream_id in ids:
                current, previous = current_rows.get(upstream_id), previous_rows.get(upstream_id)
                if previous is None:
                    kind = "added"
                    row = current
                elif current is None:
                    if source_changed:
                        continue
                    kind = "missing"
                    row = previous
                elif bytes(current["digest"]) == bytes(previous["digest"]):
                    kind = "unchanged"
                    row = current
                else:
                    kind = "changed"
                    row = current
                if diff is not None and kind != diff:
                    continue
                rows.append((upstream_id, row, kind))
            selected, more = rows[:limit], len(rows) > limit
            decisions = {}
            if selected:
                decision_rows = (await session.execute(text("SELECT * FROM catalog_decisions WHERE source_id=:source AND upstream_id = ANY(:ids)"),
                                                        {"source": source_id, "ids": [item[0] for item in selected]})).mappings().all()
                decisions = {row["upstream_id"]: row for row in decision_rows}
            items = []
            for upstream_id, row, kind in selected:
                decision = decisions.get(upstream_id)
                decision_value = None if decision is None else {
                    "version": decision["version"], "ignored": decision["ignored"],
                    "binding_id": str(decision["binding_id"]) if decision["binding_id"] else None,
                    "published_run_id": str(decision["published_run_id"]) if decision["published_run_id"] else None,
                }
                items.append(SnapshotItem(upstream_id=upstream_id, entry=_entry(row), diff=kind, decision=decision_value))
            next_cursor = None
            if more:
                next_cursor = encode_cursor({"v": 1, "source": str(source_id), "run": str(run_id),
                    "previous": str(previous_id) if previous_id else None, "after": selected[-1][0],
                    "filter": diff, "exp": int(datetime.now(timezone.utc).timestamp()) + 900}, self.cursor_key)
            return SnapshotPage(run_id=run_id, previous_run_id=previous_id, source_changed=source_changed,
                                items=tuple(items), next_cursor=next_cursor)
