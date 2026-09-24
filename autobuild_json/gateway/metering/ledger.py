"""Serialize accounting by customer/key; never hold locks during provider I/O."""
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import timedelta, timezone
import json
from uuid import uuid4

from sqlalchemy import text

from ..errors import GatewayError
from ..identity.policy import KeyPolicy
from .records import Hold
from .units import hold_micro, weighted_usage_micro

TERMINAL = {"completed", "released", "adjusted"}


class Ledger:
    def __init__(self, db, *, clock=None):
        self.db = db
        self.clock = clock

    async def _now(self, session):
        return (self.clock() if self.clock else await session.scalar(text("SELECT clock_timestamp()"))).astimezone(timezone.utc)

    async def _lock_key(self, session, key_id):
        owner = await session.scalar(text("SELECT customer_id FROM api_keys WHERE id=:id"), {"id": key_id})
        if owner is None:
            raise GatewayError("invalid_api_key", 401, "auth")
        enabled = await session.scalar(text("SELECT enabled FROM customers WHERE id=:id FOR UPDATE"), {"id": owner})
        key = (await session.execute(text("SELECT * FROM api_keys WHERE id=:id FOR UPDATE"),
                                     {"id": key_id})).mappings().one()
        return owner, enabled, key

    @asynccontextmanager
    async def _request(self, request_id, *, route=None):
        async with self.db.sessions.begin() as session:
            if route is not None:
                # Attribution INSERT takes implicit FK KEY SHARE locks. Take
                # their DB-resolved parents before accounting locks, matching
                # provider/credential -> model/binding -> customer/key writers.
                from ..providers.registry_writes import lock_model_names
                parent = (await session.execute(text("SELECT provider_id,credential_id,model_id FROM model_bindings "
                    "WHERE id=:id"), {"id": route.binding_id})).mappings().first()
                if parent is None or (parent["provider_id"], parent["credential_id"], parent["model_id"]) != (
                        route.provider_id, route.credential_id, route.public_model_id):
                    raise GatewayError("invalid_state", 409, "quota")
                for table, identity in (("providers", parent["provider_id"]), ("credentials", parent["credential_id"])):
                    await session.execute(text(f"SELECT id FROM {table} WHERE id=:id FOR KEY SHARE"), {"id": identity})
                await lock_model_names(session, (parent["model_id"],))
                await session.execute(text("SELECT id FROM model_bindings WHERE id=:id FOR KEY SHARE"),
                                      {"id": route.binding_id})
                current = (await session.execute(text("SELECT provider_id,credential_id,model_id FROM model_bindings "
                    "WHERE id=:id"), {"id": route.binding_id})).mappings().first()
                if current != parent:
                    raise GatewayError("invalid_state", 409, "quota")
            key_id = await session.scalar(text("SELECT key_id FROM requests WHERE id=:id"), {"id": request_id})
            if key_id is None:
                raise GatewayError("not_found", 404, "quota")
            _, _, key = await self._lock_key(session, key_id)
            row = (await session.execute(text("SELECT * FROM requests WHERE id=:id FOR UPDATE"),
                                         {"id": request_id})).mappings().one()
            yield session, row, key

    async def _buckets(self, session, key_id, periods, policy, delta):
        for window in ("total", "day", "month"):
            params = {"key": key_id, "window": window, "period": periods[window]}
            await session.execute(text("INSERT INTO quota_buckets(key_id,window_kind,period) VALUES (:key,:window,:period) "
                                       "ON CONFLICT DO NOTHING"), params)
            row = (await session.execute(text("SELECT spent,held FROM quota_buckets WHERE key_id=:key "
                                               "AND window_kind=:window AND period=:period FOR UPDATE"), params)).mappings().one()
            limit = getattr(policy, window + "_micro")
            if delta > 0 and limit is not None and row["spent"] + row["held"] + delta > limit:
                raise GatewayError("quota_exceeded", 429, "quota")
            await session.execute(text("UPDATE quota_buckets SET held=held+:delta WHERE key_id=:key "
                                       "AND window_kind=:window AND period=:period"), dict(params, delta=delta))

    async def reserve(self, admission):
        async with self.db.sessions.begin() as session:
            owner, enabled, key = await self._lock_key(session, admission.principal.key_id)
            now = await self._now(session)
            if (owner != admission.principal.customer_id or not enabled or not key["enabled"] or key["revoked_at"]
                    or key["version"] != admission.principal.policy_version
                    or (key["expires_at"] is not None and key["expires_at"] <= now)):
                raise GatewayError("invalid_api_key", 401, "auth")
            policy = KeyPolicy.model_validate(key["policy"])
            override = next((rate for rate in policy.model_overrides if rate.model_id == admission.model_id), None)
            input_micro = override.input_micro if override else admission.input_micro
            output_micro = override.output_micro if override else admission.output_micro
            rates = override if override else admission
            cache_read_micro = input_micro if rates.cache_read_micro is None else rates.cache_read_micro
            cache_write_micro = input_micro if rates.cache_write_micro is None else rates.cache_write_micro
            amount = hold_micro(admission.bounds, input_micro, output_micro, cache_read_micro, cache_write_micro)
            if admission.protocol not in policy.protocols or not policy.allows_model(admission.model_id):
                raise GatewayError("permission_denied", 403, "policy")
            if not now < admission.deadline <= now + timedelta(seconds=600):
                raise GatewayError("invalid_request", 400, "quota")
            await session.execute(text("UPDATE requests SET idempotency_digest=NULL, payload_digest=NULL "
                "WHERE key_id=:key AND idempotency_expires_at <= :now"), {"key": key["id"], "now": now})
            duplicate = (await session.execute(text("SELECT id,payload_digest FROM requests WHERE id=:id OR "
                "(key_id=:key AND idempotency_digest=:digest) LIMIT 1"),
                {"id": admission.request_id, "key": key["id"], "digest": admission.idempotency_digest})).mappings().first()
            if duplicate:
                if duplicate["payload_digest"] != admission.payload_digest:
                    raise GatewayError("payload_mismatch", 409, "quota")
                raise GatewayError("duplicate_request", 409, "quota")
            rpm = await session.scalar(text("SELECT count(*) FROM requests WHERE key_id=:key AND admitted_at > :cutoff"),
                                       {"key": key["id"], "cutoff": now - timedelta(seconds=60)})
            if rpm >= policy.rpm:
                raise GatewayError("rate_limited", 429, "quota", 60)
            active = await session.scalar(text("SELECT count(*) FROM requests WHERE key_id=:key "
                "AND state IN ('reserved','dispatched') AND deadline > :now"), {"key": key["id"], "now": now})
            if active >= policy.concurrency:
                raise GatewayError("concurrency_limit", 429, "quota")
            periods = {"total": "all", "day": now.strftime("%Y-%m-%d"), "month": now.strftime("%Y-%m")}
            await self._buckets(session, key["id"], periods, policy, amount)
            await session.execute(text("""INSERT INTO requests
                (id,key_id,model_id,policy_version,protocol,state,admitted_at,deadline,periods,input_micro,
                 output_micro,cache_read_micro,cache_write_micro,input_bound,output_bound,hold,
                 idempotency_digest,payload_digest,idempotency_expires_at)
                VALUES (:id,:key,:model,:version,:protocol,'reserved',:now,:deadline,CAST(:periods AS jsonb),
                        :im,:om,:cr,:cw,:ib,:ob,:hold,:digest,:payload,:expiry)"""),
                {"id": admission.request_id, "key": key["id"], "model": admission.model_id,
                 "version": key["version"], "protocol": admission.protocol, "now": now,
                 "deadline": admission.deadline, "periods": json.dumps(periods), "im": input_micro,
                 "om": output_micro, "cr": cache_read_micro, "cw": cache_write_micro,
                 "ib": admission.bounds.input_tokens, "ob": admission.bounds.output_tokens,
                 "hold": amount, "digest": admission.idempotency_digest, "payload": admission.payload_digest,
                 "expiry": now + timedelta(hours=24) if admission.idempotency_digest else None})
        return Hold(admission.request_id, admission.principal.key_id, amount)

    async def mark_dispatched(self, request_id, attempt_id, *, route=None):
        async with self._request(request_id, route=route) as (session, row, _):
            count = await session.scalar(text("SELECT count(*) FROM attempts WHERE request_id=:id"), {"id": request_id})
            if row["state"] != "reserved" or count >= 2 or row["deadline"] <= await self._now(session):
                raise GatewayError("invalid_state", 409, "quota")
            if route is not None and row["model_id"] != route.public_model_id:
                raise GatewayError("invalid_state", 409, "quota")
            if route is None:
                await session.execute(text("INSERT INTO attempts(id,request_id) VALUES (:attempt,:id)"),
                                      {"attempt": attempt_id, "id": request_id})
            else:
                await session.execute(text("INSERT INTO attempts(id,request_id,provider_id,credential_id,binding_id,config_version) "
                    "VALUES (:attempt,:id,:provider,:credential,:binding,:version)"),
                    {"attempt": attempt_id, "id": request_id, "provider": route.provider_id,
                     "credential": route.credential_id, "binding": route.binding_id, "version": route.config_version})
            await session.execute(text("UPDATE requests SET state='dispatched' WHERE id=:id"), {"id": request_id})

    async def mark_rejected(self, request_id, attempt_id, evidence):
        async with self._request(request_id) as (session, row, _):
            if row["state"] != "dispatched" or evidence != "rejected_before_generation":
                raise GatewayError("invalid_state", 409, "quota")
            result = await session.execute(text("UPDATE attempts SET status='rejected' WHERE id=:attempt "
                "AND request_id=:id AND status='started' RETURNING id"), {"attempt": attempt_id, "id": request_id})
            if result.scalar_one_or_none() is None:
                raise GatewayError("invalid_state", 409, "quota")
            await session.execute(text("UPDATE requests SET state='reserved' WHERE id=:id"), {"id": request_id})

    async def reject_and_release(self, request_id, attempt_id, evidence="rejected_before_generation"):
        """Join a locally proven rejection, including an uncertain commit ack.

        The request lock serializes dispatch and release. Earlier attempt
        evidence can never settle a later attempt's potentially active hold.
        """
        if evidence != "rejected_before_generation":
            raise GatewayError("invalid_state", 409, "quota")
        async with self._request(request_id) as (session, row, _):
            status = await session.scalar(text("SELECT status FROM attempts WHERE id=:attempt AND request_id=:id FOR UPDATE"),
                                          {"attempt": attempt_id, "id": request_id})
            if status not in {"started", "rejected"}:
                raise GatewayError("invalid_state", 409, "quota")
            superseded = await session.scalar(text("SELECT EXISTS(SELECT 1 FROM attempts other JOIN attempts target "
                "ON target.id=:attempt WHERE other.request_id=:id AND other.id<>target.id "
                "AND (other.status<>'rejected' OR other.started_at>=target.started_at))"),
                {"attempt": attempt_id, "id": request_id})
            if superseded:
                raise GatewayError("invalid_state", 409, "quota")
            if status == "started":
                await session.execute(text("UPDATE attempts SET status='rejected' WHERE id=:attempt"), {"attempt": attempt_id})
            if row["state"] == "dispatched":
                await session.execute(text("UPDATE requests SET state='reserved' WHERE id=:id"), {"id": request_id})
                row = dict(row)
                row["state"] = "reserved"
            if row["state"] == "reserved":
                await self._finalize(session, row, 0, "released", reason=evidence)
            elif row["state"] != "released":
                raise GatewayError("invalid_state", 409, "quota")

    async def resize(self, hold, bounds):
        async with self._request(hold.request_id) as (session, row, key):
            if row["state"] != "reserved" or hold.key_id != key["id"]:
                raise GatewayError("invalid_state", 409, "quota")
            amount = hold_micro(bounds, int(row["input_micro"]), int(row["output_micro"]),
                                int(row["cache_read_micro"]), int(row["cache_write_micro"]))
            await self._buckets(session, key["id"], row["periods"], KeyPolicy.model_validate(key["policy"]), amount - int(row["hold"]))
            await session.execute(text("UPDATE requests SET hold=:amount,input_bound=:ib,output_bound=:ob WHERE id=:id"),
                                  {"id": row["id"], "amount": amount, "ib": bounds.input_tokens, "ob": bounds.output_tokens})
            return Hold(row["id"], key["id"], amount)

    async def _finalize(self, session, row, charge, state, *, usage=None, computed_micro=None, actor=None, reason=None):
        for window, period in row["periods"].items():
            await session.execute(text("UPDATE quota_buckets SET held=held-:hold,spent=spent+:charge "
                "WHERE key_id=:key AND window_kind=:window AND period=:period"),
                {"hold": row["hold"], "charge": charge, "key": row["key_id"], "window": window, "period": period})
        await session.execute(text("INSERT INTO usage_ledger(id,request_id,entry_kind,amount_micro,source,actor,reason) "
            "VALUES (:entry,:id,'settlement',:amount,:source,:actor,:reason)"),
            {"entry": uuid4(), "id": row["id"], "amount": charge,
             "source": "admin_adjusted" if actor else "provider" if usage else "not_dispatched", "actor": actor, "reason": reason})
        evidence = dict(asdict(usage), computed_micro=computed_micro, charged_micro=charge) if usage else None
        await session.execute(text("UPDATE requests SET state=:state,usage=CAST(:usage AS jsonb),reason=:reason WHERE id=:id"),
            {"id": row["id"], "state": state, "usage": json.dumps(evidence) if evidence else None, "reason": reason})

    async def settle(self, request_id, usage):
        async with self._request(request_id) as (session, row, _):
            if row["state"] in TERMINAL:
                return
            if row["state"] not in {"dispatched", "usage_pending"}:
                raise GatewayError("invalid_state", 409, "quota")
            charge = weighted_usage_micro(usage, int(row["input_micro"]), int(row["output_micro"]),
                                          int(row["cache_read_micro"]), int(row["cache_write_micro"]))
            await self._finalize(session, row, min(charge, int(row["hold"])), "completed", usage=usage,
                                 computed_micro=charge,
                                 reason="usage_exceeded_bound" if charge > row["hold"] or usage.input_tokens>row['input_bound'] or usage.output_tokens>row['output_bound'] else None)

    async def release_unspent(self, request_id, evidence):
        async with self._request(request_id) as (session, row, _):
            if row["state"] in TERMINAL:
                return
            if row["state"] != "reserved" or evidence != "not_dispatched":
                raise GatewayError("invalid_state", 409, "quota")
            await self._finalize(session, row, 0, "released")

    async def mark_pending(self, request_id, reason):
        async with self._request(request_id) as (session, row, _):
            if row["state"] in TERMINAL:
                return
            if row["state"] not in {"dispatched", "usage_pending"}:
                raise GatewayError("invalid_state", 409, "quota")
            safe_reason = reason if reason in {"usage_missing", "timeout", "interrupted", "storage_error"} else "interrupted"
            await session.execute(text("UPDATE requests SET state='usage_pending',reason=:reason WHERE id=:id"),
                                  {"id": request_id, "reason": safe_reason})

    async def adjust(self, request_id, amount_micro, actor, reason):
        if (type(amount_micro) is not int or amount_micro < 0 or not actor or not reason
                or len(actor) > 100 or len(reason) > 500 or any(ord(c) < 32 for c in actor + reason)):
            raise GatewayError("invalid_request")
        async with self._request(request_id) as (session, row, _):
            if row["state"] != "usage_pending" or amount_micro > row["hold"]:
                raise GatewayError("invalid_state", 409, "quota")
            await self._finalize(session, row, amount_micro, "adjusted", actor=actor, reason=reason)
