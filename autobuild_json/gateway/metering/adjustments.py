"""Explicit append-only grants; never rewrite usage or release held quota."""
import json
from uuid import UUID, uuid4

from sqlalchemy import text

from ..accounts.quota_storage import safe_text
from ..errors import GatewayError
from ..identity.policy import KeyPolicy


class QuotaAdjustments:
    def __init__(self, db, *, actor="admin"):
        self.db, self.actor = db, actor

    async def grant(self, key_id, request_id, version, amount_micro, reason):
        if (not all(isinstance(x, UUID) for x in (key_id, request_id))
                or type(version) is not int or version < 1
                or type(amount_micro) is not int or not 0 <= amount_micro < 10**38):
            raise GatewayError("invalid_request", 422)
        safe_text(reason, 500)
        async with self.db.sessions.begin() as session:
            # One namespaced transaction lock serializes this receipt identity
            # BEFORE customer/key locks, including cross-owner ID contenders.
            # No code holding customer/key locks acquires this advisory lock.
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:name,0))"),
                                  {"name": "key-quota-grant:" + str(request_id)})
            existing = (await session.execute(text("SELECT * FROM key_quota_adjustments WHERE id=:id"),
                                              {"id": request_id})).mappings().first()
            if existing:
                if (existing["key_id"], int(existing["amount_micro"]), existing["reason"],
                        existing["version_before"]) != (key_id, amount_micro, reason, version):
                    raise GatewayError("payload_mismatch", 409, "quota")
                return self._receipt(existing)
            owner = await session.scalar(text("SELECT customer_id FROM api_keys WHERE id=:id"), {"id": key_id})
            if owner is None:
                raise GatewayError("not_found", 404)
            await session.execute(text("SELECT id FROM customers WHERE id=:id FOR UPDATE"), {"id": owner})
            row = (await session.execute(text("SELECT * FROM api_keys WHERE id=:id FOR UPDATE"),
                                         {"id": key_id})).mappings().one()
            if row["revoked_at"] is not None:
                raise GatewayError("invalid_api_key", 401, "auth")
            if row["version"] != version:
                raise GatewayError("version_conflict", 409, "quota")
            policy = KeyPolicy.model_validate(row["policy"])
            if policy.total_micro is None:
                raise GatewayError("invalid_state", 409, "quota")
            after = policy.total_micro + amount_micro
            if after >= 10**38:
                raise GatewayError("invalid_request", 422)
            await session.execute(text("UPDATE api_keys SET policy=CAST(:policy AS jsonb),version=:next WHERE id=:id"),
                {"policy": policy.model_copy(update={"total_micro": after}).model_dump_json(),
                 "next": version + 1, "id": key_id})
            result = (await session.execute(text("""INSERT INTO key_quota_adjustments
                (id,key_id,version_before,version_after,before_limit_micro,after_limit_micro,amount_micro,actor,reason)
                VALUES (:id,:key,:before,:after,:old,:new,:amount,:actor,:reason) RETURNING *"""),
                {"id": request_id, "key": key_id, "before": version, "after": version + 1,
                 "old": policy.total_micro, "new": after, "amount": amount_micro,
                 "actor": self.actor, "reason": reason})).mappings().one()
            await session.execute(text("""INSERT INTO audit_events(id,actor,action,record_id,details)
                VALUES (:id,:actor,'key.quota_granted',:record,CAST(:details AS jsonb))"""),
                {"id": uuid4(), "actor": self.actor, "record": key_id,
                 "details": json.dumps({"request_id": str(request_id), "amount_micro": str(amount_micro),
                                       "before_limit_micro": str(policy.total_micro),
                                       "after_limit_micro": str(after), "version": version + 1, "reason": reason})})
            return self._receipt(result)

    @staticmethod
    def _receipt(row):
        result = dict(row)
        for field in ("before_limit_micro", "after_limit_micro", "amount_micro"):
            result[field] = str(row[field])
        return result
