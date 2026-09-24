"""Atomic probe holds, durable evidence and money-only operator reconciliation.

No customer request, attempt or quota-ledger rows are created here. The probe
executor must close its I/O before settlement. Expired recovery is orchestrated
by ProbeService; the claim-less settlement path only accepts a safely expired
fence or an already finalized pending operation.
"""

from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING, localcontext
import hashlib
import hmac
import re
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import text

from ..errors import GatewayError, SAFE_CODES
from ..metering.budgets import money
from ..metering.costs import estimate_cost
from ..metering.records import Usage
from ..secrets import Ciphertext
from .digests import canonical_bytes
from .operations import _json, _safe_result, _view
from .probe_quote import ProbeQuotes
from .records import OperationRequest, ProbeQuote


class ProbeAccounting:
    def __init__(self, db, ops, budgets):
        self.db, self.ops, self.budgets = db, ops, budgets
        self.quotes = ProbeQuotes(db, ops.sources)

    async def _row(self, session, operation_id):
        row = (await session.execute(text("SELECT *,clock_timestamp() AS db_now FROM provider_operations "
                                          "WHERE id=:id FOR UPDATE"), {"id": operation_id})).mappings().first()
        if row is None:
            raise GatewayError("not_found", 404)
        if row["kind"] != "probe":
            raise GatewayError("invalid_state", 409)
        return row

    @staticmethod
    def _owned(row, claim, *, admission=False):
        if (row["id"] != claim.operation_id or row["source_id"] != claim.source_id
                or row["owner"] != claim.owner or row["generation"] != claim.generation
                or row["state"] != "running"):
            raise GatewayError("claim_lost", 409)
        if row["deadline"] is None or row["deadline"] <= row["db_now"]:
            raise GatewayError("operation_expired", 409)
        if admission and row["cancel_requested"]:
            raise GatewayError("operation_cancelled", 409)

    async def reserve(self, claim, quote) -> UUID:
        async with self.db.sessions.begin() as session:
            context = await self.ops.sources.lock_context(session, claim.source_id)
            row = await self._row(session, claim.operation_id)
            self._owned(row, claim, admission=True)
            try:
                request = OperationRequest.model_validate_json(_json({**row["input"], "id": str(row["id"])}))
            except (ValidationError, TypeError, ValueError):
                raise GatewayError("invalid_request") from None
            live = await self.quotes.quote_in(session, context, request.probe.binding_id)
            intent = request.probe
            if (quote != live or intent.quote_digest != live.digest or intent.max_hold != live.upper_cost
                    or intent.currency != live.currency or intent.acknowledged is not True
                    or request.expected != live.stamp or intent.expected != live.stamp
                    or row["expected"] != live.stamp.model_dump(mode="json")):
                raise GatewayError("version_conflict", 409)
            if row["attempt_id"] is not None:
                return row["attempt_id"]
            attempt_id = uuid4()
            await self.budgets.reserve_in(session, live.stamp.budget_id, attempt_id, live.currency, live.upper_cost)
            await session.execute(text("UPDATE provider_operations SET attempt_id=:attempt,budget_id=:budget,"
                "upper_cost=:hold,probe_cost_snapshot=CAST(:snapshot AS jsonb),retention_protected=true,"
                "version=version+1 WHERE id=:id"),
                {"id": row["id"], "attempt": attempt_id, "budget": live.stamp.budget_id,
                 "hold": live.upper_cost, "snapshot": live.model_dump_json()})
            return attempt_id

    async def mark_dispatched(self, claim) -> None:
        async with self.db.sessions.begin() as session:
            row = await self._row(session, claim.operation_id)
            self._owned(row, claim, admission=True)
            if row["attempt_id"] is None or row["dispatched_at"] is not None:
                raise GatewayError("invalid_state", 409)
            await session.execute(text("UPDATE provider_operations SET dispatched_at=clock_timestamp(),"
                                       "version=version+1 WHERE id=:id"), {"id": row["id"]})

    async def record_evidence(self, claim, usage: Usage | None, outcome: str, *, status=None, retry_after=None) -> None:
        if outcome not in {"success", "rejected", "uncertain"} or (usage is not None and not isinstance(usage, Usage)):
            raise GatewayError("invalid_request")
        if outcome == "rejected" and usage is not None:
            raise GatewayError("invalid_request")
        if (status is not None and (outcome != "rejected" or type(status) is not int or status not in {400, 401, 403, 404, 422, 429})
                or retry_after is not None and (status != 429 or type(retry_after) is not int or not 0 <= retry_after <= 86400)):
            raise GatewayError("invalid_request")
        payload = asdict(usage) if usage is not None else None
        async with self.db.sessions.begin() as session:
            row = await self._row(session, claim.operation_id)
            self._owned(row, claim)
            if row["dispatched_at"] is None or row["attempt_id"] is None:
                raise GatewayError("invalid_state", 409)
            previous = (row["result"] or {}).get("probe_outcome")
            if previous is not None:
                if previous != outcome or row["usage"] != payload:
                    raise GatewayError("payload_mismatch", 409)
                return
            await session.execute(text("UPDATE provider_operations SET usage=CAST(:usage AS jsonb),"
                "result=CAST(:result AS jsonb),upstream_status=:status,retry_after=:retry,version=version+1 WHERE id=:id"),
                {"id": row["id"], "usage": _json(payload) if payload is not None else None,
                 "status": status, "retry": retry_after,
                 "result": _safe_result({"probe_outcome": outcome, "rejected_before_generation": outcome == "rejected"})})

    @staticmethod
    def _actual(row):
        if row["dispatched_at"] is None:
            return Decimal(0), "cancelled" if row["cancel_requested"] else "failed", None
        outcome = (row["result"] or {}).get("probe_outcome")
        if outcome == "rejected" and row["usage"] is None:
            return Decimal(0), "failed", None
        try:
            quote = ProbeQuote.model_validate_json(_json(row["probe_cost_snapshot"]))
            usage = Usage(**row["usage"]) if row["usage"] is not None else None
            estimate = estimate_cost(usage, quote.cost_schedule)
            if estimate is None:
                return None, "usage_pending", usage
            with localcontext() as ctx:
                ctx.prec = 80
                actual = money(estimate.amount.quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING))
            within_bounds = usage.input_tokens <= quote.input_bound and usage.output_tokens <= quote.output_bound
            return actual, "succeeded" if outcome == "success" and within_bounds else "failed", usage
        except (ValidationError, ValueError, TypeError, GatewayError):
            raise GatewayError("invalid_usage", 409) from None

    async def settle(self, operation_id, *, claim=None, error=None):
        if error is not None and error not in SAFE_CODES:
            raise GatewayError("invalid_request")
        async with self.db.sessions.begin() as session:
            row = await self._row(session, operation_id)
            if row["state"] in {"succeeded", "failed", "cancelled", "stale"}:
                return _view(row)
            if row["state"] == "usage_pending" and row["finished_at"] is not None:
                if claim is not None and (claim.operation_id != row["id"] or claim.source_id != row["source_id"]
                        or claim.owner != row["owner"] or claim.generation != row["generation"]):
                    raise GatewayError("claim_lost", 409)
            elif claim is not None:
                self._owned(row, claim)
            elif row["state"] == "running":
                if row["deadline"] is None or row["deadline"] >= row["db_now"] - timedelta(seconds=15):
                    raise GatewayError("claim_lost", 409)
                await session.execute(text("UPDATE provider_operations SET generation=generation+1,owner=:owner "
                                           "WHERE id=:id"), {"id": row["id"], "owner": uuid4()})
            elif row["state"] != "usage_pending" or row["finished_at"] is None:
                raise GatewayError("claim_lost", 409)
            actual, state, usage = self._actual(row)
            if row["dispatched_at"] is None and error is not None:
                state = ("cancelled" if error == "operation_cancelled" else
                         "stale" if error in {"catalog_snapshot_stale", "version_conflict", "invalid_state"} else "failed")
            if row["state"] == "usage_pending" and actual is None:
                return _view(row)
            return await self._settle_in(session, row, actual, state, "provider_estimated", usage=usage, error=error)

    async def _settle_in(self, session, row, actual, state, source, *, usage=None, actor="system", reason=None, digest=None, error=None):
        snapshot = row["probe_cost_snapshot"] or {}
        currency = snapshot.get("currency")
        cost = {"amount": str(actual), "currency": currency, "source": source} if actual is not None else None
        details = {"amount": str(actual) if actual is not None else None, "currency": currency, "source": source}
        if reason is not None:
            details["reason"] = reason
        if usage is not None:
            stamp = snapshot.get("stamp") or {}
            binding_id = stamp.get("binding_id")
            binding_version = stamp.get("binding_version")
            if binding_id is not None and binding_version is not None and (
                    usage.input_tokens > int(snapshot.get("input_bound", usage.input_tokens))
                    or usage.output_tokens > int(snapshot.get("output_bound", usage.output_tokens))):
                binding = (await session.execute(text("SELECT version,config FROM model_bindings WHERE id=:id FOR UPDATE"),
                                                  {"id": binding_id})).mappings().first()
                if binding is not None and binding["version"] == binding_version:
                    await session.execute(text("UPDATE model_bindings SET config=jsonb_set(config,'{enabled}','false'::jsonb),version=version+1 WHERE id=:id AND version=:version"),
                                          {"id": binding_id, "version": binding_version})
                    await self._audit(session, row["id"], "catalog.probe.binding_quarantined", actor,
                                      {"binding_id": str(binding_id), "binding_version": binding_version})
                else:
                    await self._audit(session, row["id"], "catalog.probe.binding_version_changed", actor,
                                      {"binding_id": str(binding_id), "binding_version": binding_version,
                                       "current_version": binding["version"] if binding is not None else None})
        # Source/operation -> binding -> budget -> reservation, never budget -> binding.
        if row["attempt_id"] is not None:
            await self.budgets.settle_in(session, row["attempt_id"], actual)
        elif row["dispatched_at"] is not None:
            raise GatewayError("invalid_state", 409)
        if actual is not None and row["upper_cost"] is not None and actual > row["upper_cost"]:
            await self._audit(session, row["id"], "catalog.probe.cost_overrun", actor, details)
        await self._audit(session, row["id"], "catalog.probe.reconciled" if digest is not None else "catalog.probe.settled", actor, details)
        result = dict(row["result"] or {})
        result["inference_verified"] = bool(state == "succeeded" and usage is not None)
        updated = (await session.execute(text("UPDATE provider_operations SET state=:state,cost=CAST(:cost AS jsonb),"
            "settlement_source=:source,reconcile_digest=:digest,result=CAST(:result AS jsonb),"
            "error_code=:error,finished_at=clock_timestamp(),version=version+1 WHERE id=:id RETURNING *"),
            {"id": row["id"], "state": state, "cost": _json(cost) if cost is not None else None,
             "source": source if actual is not None else None, "digest": digest, "result": _safe_result(result),
             "error": "usage_pending" if state == "usage_pending" else error})).mappings().one()
        return _view(updated)

    @staticmethod
    async def _audit(session, identity, action, actor, details):
        await session.execute(text("INSERT INTO audit_events(id,actor,action,record_id,details) "
                                   "VALUES (:id,:actor,:action,:record,CAST(:details AS jsonb))"),
            {"id": uuid4(), "actor": actor, "action": action, "record": identity, "details": _json(details)})

    async def reconcile(self, operation_id, expected_version, actual: Decimal, currency: str, reason: str, actor):
        money(actual)
        if (type(expected_version) is not int or expected_version < 1 or not isinstance(currency, str)
                or not re.fullmatch(r"[A-Z]{3}", currency) or not isinstance(reason, str)
                or not 1 <= len(reason.strip()) <= 500 or len(reason) > 500
                or any(ord(c) < 32 or ord(c) == 127 for c in reason)
                or re.search(r"authorization|bearer\s|api[_ -]?key|secret|password|cookie|https?://|sk-[A-Za-z0-9]", reason, re.I)
                or not isinstance(actor, str) or not 1 <= len(actor) <= 100
                or any(ord(c) < 32 or ord(c) == 127 for c in actor)):
            raise GatewayError("invalid_request")
        payload = {"operation": str(operation_id), "version": expected_version, "actual": str(actual),
                   "currency": currency, "reason": reason, "actor": actor}
        digest = hmac.new(self.ops.sources.pepper, b"probe-reconcile-v1:" + canonical_bytes(payload), hashlib.sha256).digest()
        async with self.db.sessions.begin() as session:
            row = await self._row(session, operation_id)
            if row["reconcile_digest"] is not None:
                if not hmac.compare_digest(bytes(row["reconcile_digest"]), digest):
                    raise GatewayError("payload_mismatch", 409)
                return _view(row)
            if row["state"] != "usage_pending" or row["finished_at"] is None:
                raise GatewayError("invalid_state", 409)
            if row["version"] != expected_version:
                raise GatewayError("version_conflict", 409)
            if currency != (row["probe_cost_snapshot"] or {}).get("currency"):
                raise GatewayError("invalid_request")
            credential = (await session.execute(text("SELECT c.id,c.encrypted_secret FROM credentials c "
                "JOIN catalog_sources s ON s.credential_id=c.id WHERE s.id=:id"), {"id": row["source_id"]})).mappings().one()
            secret = self.ops.sources.vault.open("credential", credential["id"], Ciphertext.from_dict(credential["encrypted_secret"])).decode("utf-8")
            if secret and secret in reason:
                raise GatewayError("invalid_request")
            # An operator price is money-only evidence; existing provider usage is
            # preserved and unknown usage remains NULL.
            return await self._settle_in(session, row, actual, "failed", "admin_adjusted",
                                         actor=actor, reason=reason, digest=digest)
