"""Private quota transaction primitives; caller owns every session/commit."""
from datetime import datetime, timedelta
import json
import re
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import text

from ...oauth import ISSUER
from ..errors import GatewayError, SAFE_CODES
from ..routing.records import ProviderConfig
from .quota_records import CodexQuotaSnapshot, ResetClaim, ResetCreditsSnapshot, ResetResult


async def clock(session):
    return await session.scalar(text("SELECT clock_timestamp()"))


def safe_text(value, maximum):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum or not value.isprintable()
            or re.search(r"authorization|bearer\s|api[_ -]?key|secret|password|cookie|https?://|sk-[A-Za-z0-9]"
                         r"|(?:access|refresh|id)[_ -]?token", value, re.I)):
        raise GatewayError("invalid_request")


def safe_code(code):
    if code is not None and (not isinstance(code, str) or code not in SAFE_CODES):
        raise GatewayError("invalid_request")


def valid_deadline(deadline, now):
    if (not isinstance(deadline, datetime) or deadline.tzinfo is None or deadline.utcoffset() is None
            or not now < deadline <= now + timedelta(seconds=30)):
        raise GatewayError("invalid_request")


async def dependencies(session, credential_id, *, active=False, validate=True):
    provider_id = await session.scalar(text("SELECT provider_id FROM credentials WHERE id=:id"), {"id": credential_id})
    if provider_id is None:
        raise GatewayError("not_found", 404)
    provider = (await session.execute(text("SELECT id,config,version FROM providers WHERE id=:id FOR UPDATE"),
                                      {"id": provider_id})).mappings().first()
    credential = (await session.execute(text("SELECT id,provider_id,enabled,health,issuer,account_id,profile_id,"
        "version,token_generation FROM credentials WHERE id=:id FOR UPDATE"), {"id": credential_id})).mappings().first()
    if credential is None or credential["provider_id"] != provider_id or provider is None:
        raise GatewayError("invalid_state", 409)
    config = None
    try:
        config = ProviderConfig.model_validate(provider["config"])
    except ValidationError:
        if validate:
            raise GatewayError("invalid_state", 409) from None
    if validate and (credential["issuer"] != ISSUER or not credential["account_id"] or config.adapter != "codex_oauth"
                     or config.auth_mode != "oauth"):
        raise GatewayError("invalid_state", 409)
    if active and (not credential["enabled"] or credential["health"] != "active" or not config.enabled):
        raise GatewayError("invalid_state", 409)
    proxy_id = credential["profile_id"] or (config.proxy_profile_id if config else None)
    proxy = None
    if proxy_id is not None:
        proxy = (await session.execute(text("SELECT id,version FROM proxy_profiles WHERE id=:id FOR UPDATE"),
                                       {"id": proxy_id})).mappings().first()
        if proxy is None and active:
            raise GatewayError("invalid_state", 409)
    return {"provider_id": str(provider_id), "provider_version": provider["version"],
            "credential_version": credential["version"], "token_generation": credential["token_generation"],
            "proxy_id": str(proxy_id) if proxy_id else None, "proxy_version": proxy["version"] if proxy else None}


async def account_state(session, credential_id, *, create=True):
    if create:
        await session.execute(text("INSERT INTO codex_account_state(credential_id) VALUES (:id) ON CONFLICT DO NOTHING"),
                              {"id": credential_id})
    return (await session.execute(text("SELECT * FROM codex_account_state WHERE credential_id=:id FOR UPDATE"),
                                  {"id": credential_id})).mappings().first()


async def reset_row(session, operation_id):
    row = (await session.execute(text("SELECT * FROM codex_reset_requests WHERE id=:id FOR UPDATE"),
                                {"id": operation_id})).mappings().first()
    if row is None:
        raise GatewayError("not_found", 404)
    return row


async def operation_context(session, operation_id, *, active=False):
    credential_id = await session.scalar(text("SELECT credential_id FROM codex_reset_requests WHERE id=:id"),
                                         {"id": operation_id})
    if credential_id is None:
        raise GatewayError("not_found", 404)
    stamp = await dependencies(session, credential_id, active=active, validate=active)
    account = await account_state(session, credential_id)
    operation = await reset_row(session, operation_id)
    return stamp, account, operation


def result(row):
    return ResetResult(operation_id=row["id"], credential_id=row["credential_id"], version=row["version"],
                       state=row["state"], result_code=row["result_code"], upstream_status=row["upstream_status"])


def reset_claim(row, is_new):
    return ResetClaim(result=result(row), generation=row["generation"], redeem_request_id=row["redeem_request_id"],
                      is_new=is_new, deadline=row["deadline"])


def snapshot(row, kind):
    if row is None or row[kind + "_snapshot"] is None:
        return None
    record = CodexQuotaSnapshot if kind == "usage" else ResetCreditsSnapshot
    return record.model_validate_json(json.dumps(row[kind + "_snapshot"]))


def consumable(account, version, now):
    credits = snapshot(account, "credits")
    if (credits is None or account["credits_fetched_at"] is None
            or not now - timedelta(seconds=120) <= account["credits_fetched_at"] <= now):
        raise GatewayError("codex_credits_stale", 409)
    if credits.version != version:
        raise GatewayError("version_conflict", 409)
    if credits.available_count is None or credits.available_count <= 0:
        raise GatewayError("codex_no_credits", 409)


async def audit(session, row, action, *, actor=None, details=None):
    await session.execute(text("INSERT INTO audit_events(id,actor,action,record_id,details) "
        "VALUES (:id,:actor,:action,:record,CAST(:details AS jsonb))"),
        {"id": uuid4(), "actor": actor or row["actor"], "action": "codex.reset." + action,
         "record": row["id"], "details": json.dumps(details or {"state": row["state"], "version": row["version"]})})
