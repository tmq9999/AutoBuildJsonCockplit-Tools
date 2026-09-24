"""Durable, idempotent provider catalog operations and fenced claims."""

import json
import math
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ..errors import GatewayError, SAFE_CODES
from .digests import operation_digest
from .records import Claim, OperationRequest, OperationView, SourceContext


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_FORBIDDEN_KEY_PARTS = ("raw", "body", "secret", "token", "password", "credential", "header", "url")


def _safe_result(value: Any) -> str:
    """Return bounded, secret-free JSON suitable for durable operation results."""
    if not isinstance(value, dict):
        raise GatewayError("invalid_request")
    count = 0

    def visit(item, depth=0):
        nonlocal count
        count += 1
        if count > 256 or depth > 4:
            raise GatewayError("invalid_request")
        if isinstance(item, dict):
            if len(item) > 32:
                raise GatewayError("invalid_request")
            checked = {}
            for key, child in item.items():
                if not isinstance(key, str) or not _SAFE_KEY.fullmatch(key):
                    raise GatewayError("invalid_request")
                lowered = key.casefold()
                if any(part in lowered for part in _FORBIDDEN_KEY_PARTS):
                    raise GatewayError("invalid_request")
                checked[key] = visit(child, depth + 1)
            return checked
        if isinstance(item, list):
            if len(item) > 100:
                raise GatewayError("invalid_request")
            return [visit(child, depth + 1) for child in item]
        if item is None or isinstance(item, bool) or isinstance(item, int):
            if isinstance(item, int) and abs(item) >= 2**63:
                raise GatewayError("invalid_request")
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise GatewayError("invalid_request")
            return item
        if isinstance(item, str):
            if len(item) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in item):
                raise GatewayError("invalid_request")
            return item
        raise GatewayError("invalid_request")

    encoded = _json(visit(value))
    if len(encoded.encode("utf-8")) > 65536:
        raise GatewayError("invalid_request")
    return encoded


def _view(row) -> OperationView:
    usage = row.get("usage")
    if usage is not None:
        try:
            from ..metering.records import Usage
            usage = Usage(**usage)
        except (TypeError, ValueError):
            usage = None
    cost = row.get("cost")
    if isinstance(cost, dict):
        cost = cost.get("amount")
    return OperationView(
        id=row["id"], source_id=row["source_id"], kind=row["kind"], state=row["state"],
        version=row["version"], created_at=row["created_at"], deadline=row.get("deadline"),
        cancel_requested=row.get("cancel_requested", False), dispatched_at=row.get("dispatched_at"),
        usage=usage, cost=cost, result=row.get("result") or {}, error=row.get("error_code"),
    )


def _error(code: str, status: int = 409) -> GatewayError:
    return GatewayError(code if code in SAFE_CODES else "internal_error", status)


class OperationStore:
    def __init__(self, db, sources):
        self.db = db
        self.sources = sources

    async def _row(self, session, operation_id: UUID, *, lock: bool = False):
        suffix = " FOR UPDATE" if lock else ""
        return (await session.execute(text(f"SELECT * FROM provider_operations WHERE id=:id{suffix}"),
                                      {"id": operation_id})).mappings().first()

    async def get(self, client_or_canonical_id: UUID) -> OperationView:
        async with self.db.sessions() as session:
            row = await self._row(session, client_or_canonical_id)
            if row is None:
                row = (await session.execute(text("""SELECT o.* FROM provider_operation_claims c
                    JOIN provider_operations o ON o.id=c.operation_id WHERE c.client_id=:id"""),
                                           {"id": client_or_canonical_id})).mappings().first()
        if row is None:
            raise GatewayError("not_found", 404)
        return _view(row)

    async def enqueue(self, request: OperationRequest, actor) -> OperationView:
        digest = operation_digest(request)
        async with self.db.sessions.begin() as session:
            # The client claim is authoritative. This lookup deliberately precedes
            # source/context validation so completed jobs remain replayable after disable.
            claim = (await session.execute(text("SELECT operation_id,payload_digest FROM provider_operation_claims "
                                               "WHERE client_id=:client"), {"client": request.id})).mappings().first()
            if claim is not None:
                if bytes(claim["payload_digest"]) != digest:
                    raise _error("payload_mismatch")
                row = await self._row(session, claim["operation_id"])
                if row is None:
                    raise GatewayError("storage_unavailable", 503, "storage")
                return _view(row)

            context = await self.sources.lock_context(session, request.source_id)
            if context is None:
                raise GatewayError("concurrency_limit", 409)
            if context.stamp != request.expected:
                raise _error("version_conflict")
            return await self.enqueue_in(session, request, actor, context)

    async def enqueue_in(self, session, request: OperationRequest, actor,
                         context: SourceContext) -> OperationView:
        digest = operation_digest(request)
        active = (await session.execute(text("""SELECT * FROM provider_operations
            WHERE source_id=:source AND state IN ('queued','running') FOR UPDATE"""),
            {"source": request.source_id})).mappings().first()
        if active is not None:
            if bytes(active["payload_digest"]) != digest or active["kind"] != request.kind:
                raise _error("operation_conflict")
            await self._claim_client(session, request.id, active["id"], digest)
            return _view(active)

        payload = request.model_dump(mode="json", exclude={"id"})
        expected = request.expected.model_dump(mode="json")
        try:
            async with session.begin_nested():
                row = (await session.execute(text("""INSERT INTO provider_operations
                    (id,source_id,kind,state,payload_digest,input,expected,queued_expires_at)
                    VALUES (:id,:source,:kind,'queued',:digest,CAST(:input AS jsonb),CAST(:expected AS jsonb),
                            clock_timestamp()+interval '10 minutes') RETURNING *"""),
                    {"id": request.id, "source": request.source_id, "kind": request.kind,
                     "digest": digest, "input": _json(payload), "expected": _json(expected)})).mappings().one()
        except IntegrityError:
            existing = await self._row(session, request.id)
            if existing is None:
                raise GatewayError("storage_unavailable", 503, "storage")
            if bytes(existing["payload_digest"]) != digest:
                raise _error("payload_mismatch")
            await self._claim_client(session, request.id, existing["id"], digest)
            return _view(existing)
        await self._claim_client(session, request.id, row["id"], digest)
        return _view(row)

    async def _claim_client(self, session, client_id: UUID, operation_id: UUID, digest: bytes) -> None:
        try:
            async with session.begin_nested():
                await session.execute(text("INSERT INTO provider_operation_claims(client_id,operation_id,payload_digest) "
                                          "VALUES (:client,:operation,:digest)"),
                                      {"client": client_id, "operation": operation_id, "digest": digest})
        except IntegrityError:
            existing = (await session.execute(text("SELECT operation_id,payload_digest FROM provider_operation_claims "
                                                    "WHERE client_id=:client"), {"client": client_id})).mappings().first()
            if existing is None:
                raise GatewayError("storage_unavailable", 503, "storage")
            if bytes(existing["payload_digest"]) != digest:
                raise _error("payload_mismatch")
            return

    async def claim(self, operation_id: UUID, owner: UUID) -> Claim | None:
        # Read the bounded provider timeout before taking the operation lock. The
        # context read is deliberately outside the claim transaction so no source
        # or provider lock is held while the operation row is updated.
        try:
            async with self.db.sessions() as lookup:
                operation = (await lookup.execute(text("SELECT * FROM provider_operations WHERE id=:id"),
                                                  {"id": operation_id})).mappings().first()
            if operation is None or operation["state"] != "queued":
                return None
            expected = operation["expected"]
            if operation["queued_expires_at"] <= datetime.now(timezone.utc):
                await self._stale(operation_id, "operation_expired")
                return None
            source_id = operation["source_id"]
            from .records import ConfigStamp
            expected_stamp = ConfigStamp.model_validate(expected)
            context = await self.sources.context(source_id)
            timeout = context.route.timeout
            if context.stamp != expected_stamp:
                await self._stale(operation_id, "version_conflict")
                return None
        except GatewayError as exc:
            await self._stale(operation_id, "invalid_state" if exc.code != "not_found" else "not_found")
            return None
        async with self.db.sessions.begin() as session:
            row = (await session.execute(text("""UPDATE provider_operations SET state='running',owner=:owner,
                generation=generation+1,started_at=clock_timestamp(),heartbeat_at=clock_timestamp(),
                deadline=clock_timestamp()+make_interval(secs=>LEAST(:timeout,60)),version=version+1
                WHERE id=:id AND state='queued' AND queued_expires_at>clock_timestamp()
                RETURNING id,source_id,generation,deadline"""),
                {"id": operation_id, "owner": owner, "timeout": timeout})).mappings().first()
            if row is None:
                return None
            return Claim(operation_id=row["id"], source_id=row["source_id"], owner=owner,
                         generation=row["generation"], deadline=row["deadline"])

    async def _stale(self, operation_id: UUID, code: str) -> None:
        async with self.db.sessions.begin() as session:
            await session.execute(text("""UPDATE provider_operations SET state='stale',error_code=:code,
                finished_at=clock_timestamp(),version=version+1
                WHERE id=:id AND state='queued'"""), {"id": operation_id, "code": code})

    async def heartbeat(self, claim: Claim) -> bool:
        async with self.db.sessions.begin() as session:
            updated = await session.scalar(text("""UPDATE provider_operations SET heartbeat_at=clock_timestamp()
                WHERE id=:id AND owner=:owner AND generation=:generation AND state='running'
                  AND deadline>clock_timestamp() AND NOT cancel_requested RETURNING id"""),
                {"id": claim.operation_id, "owner": claim.owner, "generation": claim.generation})
            return updated is not None

    async def assert_claim(self, session, claim: Claim) -> OperationView:
        row = (await session.execute(text("SELECT *, clock_timestamp() AS _db_now FROM provider_operations WHERE id=:id FOR UPDATE"),
                                     {"id": claim.operation_id})).mappings().first()
        if row is None:
            raise GatewayError("not_found", 404)
        if (row["owner"] != claim.owner or row["generation"] != claim.generation
                or row["state"] != "running"):
            raise _error("claim_lost")
        if row["deadline"] is None or row["deadline"] <= row["_db_now"]:
            raise _error("operation_expired")
        if row["cancel_requested"]:
            raise _error("operation_cancelled")
        return _view(row)

    async def read_claim(self, claim: Claim) -> OperationView:
        async with self.db.sessions() as session:
            return await self.assert_claim(session, claim)

    async def cancel(self, operation_id: UUID, expected_version: int, actor) -> OperationView:
        async with self.db.sessions.begin() as session:
            row = await self._row(session, operation_id, lock=True)
            if row is None:
                raise GatewayError("not_found", 404)
            if row["version"] != expected_version:
                raise _error("version_conflict")
            if row["state"] == "queued":
                await session.execute(text("UPDATE provider_operations SET state='cancelled',finished_at=clock_timestamp(),version=version+1 WHERE id=:id"),
                                      {"id": operation_id})
            elif row["state"] == "running" and not row["cancel_requested"]:
                await session.execute(text("UPDATE provider_operations SET cancel_requested=true,version=version+1 WHERE id=:id"),
                                      {"id": operation_id})
            return _view(await self._row(session, operation_id))

    async def finish(self, claim: Claim, state: str, result=None, error=None) -> OperationView:
        if state not in {"succeeded", "failed", "cancelled", "stale", "usage_pending"}:
            raise GatewayError("invalid_request")
        code = error.code if isinstance(error, GatewayError) else error
        code = code if isinstance(code, str) and code in SAFE_CODES else ("internal_error" if code else None)
        async with self.db.sessions.begin() as session:
            updated = (await session.execute(text("""UPDATE provider_operations SET state=:state,
                result=CAST(:result AS jsonb),error_code=:code,finished_at=clock_timestamp(),version=version+1
                WHERE id=:id AND owner=:owner AND generation=:generation AND state='running'
                  AND deadline>clock_timestamp() AND NOT cancel_requested RETURNING *"""),
                {"state": state, "result": _safe_result(result) if result is not None else None, "code": code,
                 "id": claim.operation_id, "owner": claim.owner, "generation": claim.generation})).mappings().first()
            if updated is None:
                raise _error("claim_lost")
            return _view(updated)

    async def mark_dispatched(self, claim: Claim, attempt_id: UUID) -> None:
        async with self.db.sessions.begin() as session:
            updated = await session.scalar(text("""UPDATE provider_operations SET dispatched_at=clock_timestamp(),attempt_id=:attempt
                WHERE id=:id AND owner=:owner AND generation=:generation AND state='running'
                  AND deadline>clock_timestamp() AND NOT cancel_requested RETURNING id"""),
                {"id": claim.operation_id, "owner": claim.owner, "generation": claim.generation,
                 "attempt": attempt_id})
            if updated is None:
                raise _error("claim_lost")
