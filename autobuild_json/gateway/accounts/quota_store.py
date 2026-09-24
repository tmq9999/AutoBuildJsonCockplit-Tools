"""Storage-only, fenced Codex quota refresh and explicit reset operations."""
import asyncio
from datetime import timedelta
import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import text

from ..errors import GatewayError
from .quota_records import CodexQuotaSnapshot, RefreshClaim, ResetCreditsSnapshot
from . import quota_storage as storage


class QuotaStore:
    def __init__(self, db):
        self.db = db

    async def _read(self, credential_id, kind):
        async with self.db.sessions.begin() as session:
            await storage.dependencies(session, credential_id)
            row = await storage.account_state(session, credential_id, create=False)
            result = storage.snapshot(row, kind)
            if result is not None and kind == "usage":
                result = result.model_copy(update={"last_error": row["usage_error"]})
            return result

    async def read(self, credential_id):
        return await self._read(credential_id, "usage")

    async def read_credits(self, credential_id):
        return await self._read(credential_id, "credits")

    async def get_reset(self, operation_id):
        async with self.db.sessions() as session:
            row = (await session.execute(text("SELECT * FROM codex_reset_requests WHERE id=:id"),
                                         {"id": operation_id})).mappings().first()
            return storage.result(row) if row else None

    async def begin_refresh(self, credential_id, deadline):
        async with self.db.sessions.begin() as session:
            stamp = await storage.dependencies(session, credential_id, active=True)
            await storage.account_state(session, credential_id)
            now = await storage.clock(session)
            storage.valid_deadline(deadline, now)
            generation = await session.scalar(text("UPDATE codex_account_state SET generation=generation+1,"
                "refresh_deadline=:deadline,refresh_started_at=:now,last_attempt_at=:now WHERE credential_id=:id RETURNING generation"),
                {"deadline": deadline, "now": now, "id": credential_id})
            return RefreshClaim(credential_id=credential_id, generation=generation, deadline=deadline,
                                **{**stamp, "provider_id": UUID(stamp["provider_id"]),
                                   "proxy_id": UUID(stamp["proxy_id"]) if stamp["proxy_id"] else None})

    async def save_refresh(self, claim, usage, credits, error_codes):
        if not isinstance(claim, RefreshClaim) or not isinstance(error_codes, (tuple, list)) or len(error_codes) > 2:
            raise GatewayError("invalid_request")
        for code in error_codes:
            if code is None:
                raise GatewayError("invalid_request")
            storage.safe_code(code)
        for value, record in ((usage, CodexQuotaSnapshot), (credits, ResetCreditsSnapshot)):
            if value is not None and (not isinstance(value, record) or value.credential_id != claim.credential_id):
                raise GatewayError("invalid_request")
        async with self.db.sessions.begin() as session:
            try:
                stamp = await storage.dependencies(session, claim.credential_id, active=True)
            except GatewayError:
                return False
            row = await storage.account_state(session, claim.credential_id)
            now = await storage.clock(session)
            expected = claim.model_dump(mode="json", exclude={"credential_id", "generation", "deadline"})
            if (stamp != expected or row["generation"] != claim.generation or row["refresh_deadline"] != claim.deadline
                    or now >= claim.deadline):
                return False
            if any(value is not None and not row["refresh_started_at"] <= value.fetched_at <= now for value in (usage, credits)):
                raise GatewayError("invalid_request")
            version = row["version"] + 1
            # Versions come from the durable account, never caller-controlled counters.
            saved_usage = usage.model_copy(update={"version": version, "last_error": None}) if usage else None
            saved_credits = credits.model_copy(update={"version": version}) if credits else None
            usage_error = None if usage else (error_codes[0] if error_codes else "codex_usage_unavailable")
            credits_error = None if credits else (error_codes[-1] if error_codes else "codex_credits_unavailable")
            failed = usage is None or credits is None
            await session.execute(text("""UPDATE codex_account_state SET version=:version,
                usage_snapshot=COALESCE(CAST(:usage AS jsonb),usage_snapshot),
                credits_snapshot=COALESCE(CAST(:credits AS jsonb),credits_snapshot),
                fetched_at=CASE WHEN :has_usage THEN :usage_at ELSE fetched_at END,
                credits_fetched_at=CASE WHEN :has_credits THEN :credits_at ELSE credits_fetched_at END,
                usage_generation=CASE WHEN :has_usage THEN generation ELSE usage_generation END,
                credits_generation=CASE WHEN :has_credits THEN generation ELSE credits_generation END,
                snapshot_refresh_started_at=CASE WHEN NOT :failed THEN refresh_started_at ELSE snapshot_refresh_started_at END,
                last_attempt_at=:now,refresh_deadline=NULL,usage_error=:usage_error,credits_error=:credits_error,
                last_error=:error,last_failure_at=CASE WHEN :failed THEN :now ELSE last_failure_at END,
                last_success_at=CASE WHEN NOT :failed THEN :now ELSE last_success_at END,
                failure_count=CASE WHEN :failed THEN failure_count+1 ELSE 0 END WHERE credential_id=:id"""),
                {"version": version, "usage": saved_usage.model_dump_json() if saved_usage else None,
                 "credits": saved_credits.model_dump_json() if saved_credits else None,
                 "usage_at": usage.fetched_at if usage else None, "credits_at": credits.fetched_at if credits else None,
                 "has_usage": usage is not None, "has_credits": credits is not None, "failed": failed,
                 "usage_error": usage_error, "credits_error": credits_error, "error": usage_error or credits_error,
                 "now": now, "id": claim.credential_id})
            return True

    async def prepare_reset(self, credential_id, request_id, credits_version, actor, *, deadline):
        storage.safe_text(actor, 100)
        if type(credits_version) is not int or credits_version < 1:
            raise GatewayError("invalid_request")
        digest = hashlib.sha256(f"{credential_id}:{credits_version}".encode()).digest()
        async with self.db.sessions.begin() as session:
            stamp = await storage.dependencies(session, credential_id, validate=False)
            account = await storage.account_state(session, credential_id)
            existing = (await session.execute(text("SELECT * FROM codex_reset_requests WHERE id=:id"),
                                              {"id": request_id})).mappings().first()
            if existing:
                if bytes(existing["payload_digest"]) != digest:
                    raise GatewayError("payload_mismatch", 409)
                return storage.reset_claim(existing, False)
            # Dependencies are already held in canonical order. Revalidation does
            # not acquire any new row; it checks enabled/OAuth health for a new POST.
            stamp = await storage.dependencies(session, credential_id, active=True)
            now = await storage.clock(session)
            storage.valid_deadline(deadline, now)
            if await session.scalar(text("SELECT EXISTS(SELECT 1 FROM codex_reset_requests WHERE credential_id=:id "
                "AND state IN ('prepared','dispatched','unknown','succeeded_refresh_failed'))"), {"id": credential_id}):
                raise GatewayError("operation_conflict", 409)
            storage.consumable(account, credits_version, now)
            row = (await session.execute(text("""INSERT INTO codex_reset_requests
                (id,credential_id,redeem_request_id,payload_digest,state,actor,requested_at,deadline,credits_version,stamp)
                VALUES (:id,:credential,:redeem,:digest,'prepared',:actor,:now,:deadline,:version,CAST(:stamp AS jsonb))
                ON CONFLICT (id) DO NOTHING RETURNING *"""),
                {"id": request_id, "credential": credential_id, "redeem": uuid4(), "digest": digest,
                 "actor": actor, "now": now, "deadline": deadline, "version": credits_version, "stamp": json.dumps(stamp)})).mappings().first()
            if row is None:
                # A cross-account same-ID contender may win after our initial read.
                # Never acquire that other account's locks while holding this one.
                existing = (await session.execute(text("SELECT * FROM codex_reset_requests WHERE id=:id"),
                                                  {"id": request_id})).mappings().one()
                if bytes(existing["payload_digest"]) != digest:
                    raise GatewayError("payload_mismatch", 409)
                return storage.reset_claim(existing, False)
            await storage.audit(session, row, "prepared")
            return storage.reset_claim(row, True)

    async def mark_dispatched(self, operation_id, generation):
        async with self.db.sessions.begin() as session:
            try:
                stamp, account, row = await storage.operation_context(session, operation_id, active=True)
            except GatewayError:
                return False
            now = await storage.clock(session)
            if row["state"] != "prepared" or row["generation"] != generation or row["deadline"] <= now or row["stamp"] != stamp:
                return False
            try:
                storage.consumable(account, row["credits_version"], now)
            except GatewayError:
                return False
            row = (await session.execute(text("UPDATE codex_reset_requests SET state='dispatched',version=version+1,"
                "dispatched_at=:now,dispatch_generation=:generation WHERE id=:id RETURNING *"),
                {"now": now, "generation": account["generation"], "id": operation_id})).mappings().one()
            await storage.audit(session, row, "dispatched")
            return True

    async def finish_reset(self, operation_id, generation, state, code, status):
        storage.safe_code(code)
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise GatewayError("invalid_request")
        if state not in {"rejected", "unknown", "succeeded_refresh_failed", "succeeded"}:
            raise GatewayError("invalid_state", 409)
        async with self.db.sessions.begin() as session:
            stamp, account, row = await storage.operation_context(session, operation_id, active=True)
            now = await storage.clock(session)
            if row["generation"] != generation or row["state"] not in {"dispatched", "succeeded_refresh_failed"} \
                    or row["stamp"] != stamp:
                raise GatewayError("claim_lost", 409)
            if state == "succeeded":
                if (row["state"] != "succeeded_refresh_failed" or account["snapshot_refresh_started_at"] is None
                        or account["snapshot_refresh_started_at"] < row["receipt_at"]
                        or account["usage_generation"] != account["credits_generation"]
                        or account["usage_generation"] <= row["dispatch_generation"]
                        or account["usage_error"] or account["credits_error"]
                        or any(value is None or not now - timedelta(seconds=120) <= value <= now
                               for value in (account["fetched_at"], account["credits_fetched_at"]))
                        or status is not None and status != row["upstream_status"]):
                    raise GatewayError("invalid_state", 409)
            elif row["state"] != "dispatched" or row["deadline"] <= now:
                raise GatewayError("claim_lost", 409)
            if state == "succeeded_refresh_failed" and (status is None or not 200 <= status < 300):
                raise GatewayError("invalid_state", 409)
            row = (await session.execute(text("UPDATE codex_reset_requests SET state=:state,version=version+1,"
                "result_code=:code,upstream_status=COALESCE(:status,upstream_status),completed_at=:now,"
                "receipt_at=CASE WHEN :state='succeeded_refresh_failed' THEN :now ELSE receipt_at END WHERE id=:id RETURNING *"),
                {"state": state, "code": code, "status": status, "now": now, "id": operation_id})).mappings().one()
            await storage.audit(session, row, state)
            return storage.result(row)

    async def recover_expired(self):
        count = 0
        # Independent per-account transactions avoid holding one account while
        # requesting a second provider. Whole cleanup is bounded to five seconds.
        try:
            async with asyncio.timeout(5):
                async with self.db.sessions() as session:
                    ids = (await session.execute(text("SELECT id FROM codex_reset_requests WHERE state IN ('prepared','dispatched') "
                        "AND deadline<=clock_timestamp() ORDER BY credential_id,id LIMIT 100"))).scalars().all()
                for operation_id in ids:
                    async with self.db.sessions.begin() as session:
                        _, _, row = await storage.operation_context(session, operation_id)
                        now = await storage.clock(session)
                        if row["state"] not in {"prepared", "dispatched"} or row["deadline"] > now:
                            continue
                        state = "rejected" if row["state"] == "prepared" else "unknown"
                        code = "operation_expired" if state == "rejected" else "codex_reset_uncertain"
                        row = (await session.execute(text("UPDATE codex_reset_requests SET state=:state,result_code=:code,"
                            "generation=generation+1,version=version+1,completed_at=:now WHERE id=:id RETURNING *"),
                            {"state": state, "code": code, "now": now, "id": operation_id})).mappings().one()
                        await storage.audit(session, row, "recovered", actor="system")
                    count += 1
        except TimeoutError:
            pass
        return count

    async def resolve_reset(self, operation_id, version, outcome, reason, actor):
        storage.safe_text(actor, 100)
        storage.safe_text(reason, 500)
        if type(version) is not int or version < 1 or outcome not in {"confirmed_applied", "confirmed_not_applied"}:
            raise GatewayError("invalid_request")
        async with self.db.sessions.begin() as session:
            _, _, row = await storage.operation_context(session, operation_id)
            if row["version"] != version:
                raise GatewayError("version_conflict", 409)
            if row["state"] not in {"unknown", "succeeded_refresh_failed"}:
                raise GatewayError("invalid_state", 409)
            state = "succeeded" if outcome == "confirmed_applied" else "rejected"
            code = "codex_reset_applied" if outcome == "confirmed_applied" else "codex_reset_not_applied"
            now = await storage.clock(session)
            resolution = {"outcome": outcome, "reason": reason, "actor": actor, "resolved_at": now.isoformat()}
            row = (await session.execute(text("UPDATE codex_reset_requests SET state=:state,result_code=:code,version=version+1,"
                "generation=generation+1,resolution=CAST(:resolution AS jsonb),completed_at=:now WHERE id=:id RETURNING *"),
                {"state": state, "code": code, "resolution": json.dumps(resolution), "now": now, "id": operation_id})).mappings().one()
            await session.execute(text("UPDATE codex_account_state SET credits_fetched_at=NULL,generation=generation+1,"
                "refresh_deadline=NULL,version=version+1 WHERE credential_id=:id"), {"id": row["credential_id"]})
            await storage.audit(session, row, "resolved", actor=actor, details=resolution)
            return storage.result(row)
