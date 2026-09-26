"""Private account views and explicit commands. GETs never contact providers."""
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import Query, Response
from sqlalchemy import text

from ...oauth import ISSUER
from ..accounts.imports import CODEX_PROVIDER
from ..accounts import quota_storage as storage
from ..errors import GatewayError
from ..providers.codex_capabilities import codex_runtime_capabilities
from ..routing.records import ProviderConfig
from ..settings import ServiceSettings
from .codex_schemas import AccountUpdate, CodexProxyInput, PoolInput, QuotaAdjustInput, RefreshInput, ResetConsumeInput, ResetResolveInput
from .usage import UsageReports, report_window


class ServiceStatus:
    """Stored configuration/accounting, deliberately not a liveness probe."""

    def __init__(self, services):
        self.services = services

    async def read(self):
        status, base_url = "configured", None
        try:
            settings = self.services.service_settings or ServiceSettings()
            # A public proxy's external TLS origin cannot be inferred from a
            # bind address. Only advertise an unambiguous local endpoint.
            host = {"127.0.0.1": "127.0.0.1", "localhost": "localhost", "::1": "[::1]",
                    "0.0.0.0": "127.0.0.1", "::": "[::1]"}.get(settings.host)
            if host is not None:
                base_url = f"http://{host}:{settings.port}/v1"
            else:
                status = "unavailable"
        except ValueError:
            status = "unavailable"

        async with self.services.db.sessions.begin() as session:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            accounts = dict((await session.execute(text("""SELECT count(*) AS total,
                count(*) FILTER (WHERE c.enabled AND c.health='active') AS active,
                count(*) FILTER (WHERE c.enabled AND c.health='reauth_required') AS reauth_required,
                count(*) FILTER (WHERE c.enabled AND c.health='refresh_uncertain') AS refresh_uncertain,
                count(*) FILTER (WHERE c.enabled AND c.health='unverified') AS unverified,
                count(*) FILTER (WHERE NOT c.enabled) AS disabled
                FROM credentials c JOIN providers p ON p.id=c.provider_id
                WHERE c.issuer=:issuer AND c.account_id IS NOT NULL AND c.account_id<>''
                AND p.config->>'adapter'='codex_oauth' AND p.config->>'auth_mode'='oauth'"""),
                {"issuer": ISSUER})).mappings().one())
            keys = dict((await session.execute(text("""SELECT count(*) AS total,
                count(*) FILTER (WHERE k.enabled AND k.revoked_at IS NULL AND c.enabled
                    AND (k.expires_at IS NULL OR k.expires_at>now())) AS enabled
                FROM api_keys k JOIN customers c ON c.id=k.customer_id"""))).mappings().one())
            usage = dict((await session.execute(text("""SELECT count(*) AS requests,
                count(*) FILTER (WHERE state='completed') AS completed,
                count(*) FILTER (WHERE state='released') AS failed,
                count(*) FILTER (WHERE state IN ('reserved','dispatched','usage_pending')) AS pending
                FROM requests"""))).mappings().one())
            counters = ("input_tokens", "cached_read", "cached_write", "output_tokens")
            # A completed request must have one serving attempt AND an
            # immutable settlement. Rejected/retried/pending attempts cannot
            # contribute. Missing legacy fields remain unknown, not zero.
            sums = ",".join(f"CASE WHEN count(*)=0 THEN 0 WHEN count(usage->>'{name}')=count(*) "
                            f"THEN sum((usage->>'{name}')::numeric) ELSE NULL END AS {name}" for name in counters)
            totals = (await session.execute(text("""WITH settled AS (
                SELECT CASE WHEN a.completed_count=1 AND l.id IS NOT NULL THEN a.usage ELSE NULL END AS usage,
                    CASE WHEN a.completed_count=1 THEN l.amount_micro ELSE NULL END AS charged_micro
                FROM requests r
                LEFT JOIN LATERAL (SELECT x.usage,count(*) OVER () AS completed_count
                    FROM attempts x WHERE x.request_id=r.id AND x.status='completed'
                    ORDER BY x.started_at DESC,x.id DESC LIMIT 1) a ON true
                LEFT JOIN usage_ledger l ON l.request_id=r.id AND l.entry_kind='settlement'
                WHERE r.state='completed'
            ) SELECT """ + sums + """,CASE WHEN count(*)=0 THEN 0
                WHEN count(charged_micro)=count(*) THEN sum(charged_micro) ELSE NULL END AS charged_micro
                FROM settled"""))).mappings().one()
            usage.update({name: str(value) if value is not None else None for name, value in totals.items()})
        return {"status": status, "base_url": base_url, "accounts": accounts, "keys": keys,
                "usage": usage, "version": codex_runtime_capabilities()["version"]}


class CodexProxy:
    """Private projection for the default Codex egress profile.

    Only profile identity and non-sensitive mode/name are exposed.  Individual
    credential overrides continue to take precedence in route selection.
    """

    def __init__(self, services):
        self.services = services

    async def read(self):
        async with self.services.db.sessions() as session:
            row = (await session.execute(text("""SELECT p.config,p.version,
                    x.id AS profile_id,x.name AS profile_name,x.config AS profile_config
                    FROM providers p LEFT JOIN proxy_profiles x
                      ON x.id::text=p.config->>'proxy_profile_id'
                    WHERE p.id=:id"""), {"id": CODEX_PROVIDER})).mappings().first()
        if row is None:
            raise GatewayError("not_found", 404)
        provider = ProviderConfig.model_validate(row["config"])
        if provider.adapter != "codex_oauth" or provider.auth_mode != "oauth":
            raise GatewayError("not_found", 404)
        profile = None
        if row["profile_id"] is not None:
            profile = {"id": row["profile_id"], "name": row["profile_name"],
                       "mode": (row["profile_config"] or {}).get("mode", "unknown")}
        return {"provider_id": CODEX_PROVIDER, "version": row["version"],
                "proxy_profile_id": provider.proxy_profile_id, "profile": profile,
                "source": "provider" if provider.proxy_profile_id else "direct"}

    async def update(self, payload):
        async with self.services.db.sessions.begin() as session:
            # Lock provider -> new profile, matching account updates and the
            # profile-deletion reference check. Never lock the old profile.
            row = (await session.execute(text("SELECT config,version FROM providers WHERE id=:id FOR UPDATE"),
                                        {"id": CODEX_PROVIDER})).mappings().first()
            if row is None:
                raise GatewayError("not_found", 404)
            provider = ProviderConfig.model_validate(row["config"])
            if provider.adapter != "codex_oauth" or provider.auth_mode != "oauth":
                raise GatewayError("not_found", 404)
            if row["version"] != payload.version:
                raise GatewayError("version_conflict", 409)
            if payload.proxy_profile_id is not None and not await session.scalar(
                    text("SELECT id FROM proxy_profiles WHERE id=:id FOR KEY SHARE"),
                    {"id": payload.proxy_profile_id}):
                raise GatewayError("invalid_request", 422)
            # This endpoint owns exactly one field. Re-serializing the parsed
            # model would fill defaults/normalize unrelated stored settings.
            updated = dict(row["config"], proxy_profile_id=(
                str(payload.proxy_profile_id) if payload.proxy_profile_id else None))
            config = json.dumps(updated)
            next_version = row["version"] + 1
            await session.execute(text("UPDATE providers SET config=CAST(:config AS jsonb),version=:version WHERE id=:id"),
                                  {"id": CODEX_PROVIDER, "config": config, "version": next_version})
            await session.execute(text("INSERT INTO config_versions(provider_id,version,config) "
                                       "VALUES (:id,:version,CAST(:config AS jsonb))"),
                                  {"id": CODEX_PROVIDER, "version": next_version, "config": config})
            await self.services.identity._audit(session, "codex.service_proxy.updated", CODEX_PROVIDER,
                {"version": next_version, "proxy_profile_id": updated["proxy_profile_id"]})
        return {"provider_id": CODEX_PROVIDER, "version": next_version,
                "proxy_profile_id": payload.proxy_profile_id,
                "source": "provider" if payload.proxy_profile_id else "direct"}


class AccountViews:
    def __init__(self, services):
        self.services, self.db = services, services.db

    async def list(self, limit, after):
        async with self.db.sessions() as session:
            rows = (await session.execute(text("""SELECT c.id,c.email,c.enabled,c.health,c.profile_id,c.version,
                a.usage_snapshot,p.name AS proxy_profile_name FROM credentials c
                JOIN providers upstream ON upstream.id=c.provider_id
                LEFT JOIN codex_account_state a ON a.credential_id=c.id
                LEFT JOIN proxy_profiles p ON p.id=c.profile_id
                WHERE c.issuer=:issuer AND c.account_id IS NOT NULL AND c.account_id<>''
                AND upstream.config->>'adapter'='codex_oauth' AND upstream.config->>'auth_mode'='oauth'
                AND (CAST(:after AS uuid) IS NULL OR c.id>CAST(:after AS uuid)) ORDER BY c.id LIMIT :limit"""),
                {"issuer": ISSUER, "after": after, "limit": limit + 1})).mappings().all()
        # Full email is an operator-visible identity on this private,
        # authenticated projection; tokens and provider account IDs stay out.
        return {"items": [{"id": r["id"], "email": r["email"], "enabled": r["enabled"],
                           "status": r["health"], "version": r["version"],
                           "plan_type": (r["usage_snapshot"] or {}).get("plan_type"),
                           "proxy_profile_id": r["profile_id"], "proxy_profile_name": r["proxy_profile_name"]}
                          for r in rows[:limit]],
                "next_after": rows[limit - 1]["id"] if len(rows) > limit else None}

    async def projection(self, identity, kind):
        async with self.db.sessions.begin() as session:
            await storage.dependencies(session, identity)
            row = await storage.account_state(session, identity, create=False)
            snapshot = storage.snapshot(row, kind)
            now = await storage.clock(session)
            fetched = row["fetched_at" if kind == "usage" else "credits_fetched_at"] if row else None
            error = row[kind + "_error"] if row else None
            if snapshot is not None:
                snapshot = snapshot.model_dump(mode="json")
                if kind == "credits":
                    snapshot["credits"] = [{"state": r["state"], "expires_at": r["expires_at"]}
                                           for r in snapshot["credits"]]
                else:
                    snapshot["last_error"] = error
            result = {"snapshot": snapshot, "stale": fetched is None or not now - timedelta(seconds=120) <= fetched <= now or error is not None,
                      "last_error": error, "fetched_at": fetched,
                      "last_attempt_at": row["last_attempt_at"] if row else None,
                      "version": row["version"] if row else None}
            if kind == "credits":
                active = (await session.execute(text("SELECT * FROM codex_reset_requests WHERE credential_id=:id "
                    "AND state IN ('prepared','dispatched','unknown','succeeded_refresh_failed')"),
                    {"id": identity})).mappings().first()
                result["active_reset"] = storage.result(active).model_dump(mode="json") if active else None
                last = (await session.execute(text("SELECT * FROM codex_reset_requests WHERE credential_id=:id "
                    "ORDER BY requested_at DESC,id DESC LIMIT 1"), {"id": identity})).mappings().first()
                result["last_reset"] = storage.result(last).model_dump(mode="json") if last else None
            return result

    async def update(self, identity, payload):
        async with self.db.sessions.begin() as session:
            # Configuration-only change: lock provider -> credential -> NEW
            # profile, never both old and new profiles. No dispatch stamp needed.
            provider_id = await session.scalar(text("SELECT provider_id FROM credentials WHERE id=:id"), {"id": identity})
            if provider_id is None:
                raise GatewayError("not_found", 404)
            config = await session.scalar(text("SELECT config FROM providers WHERE id=:id FOR UPDATE"), {"id": provider_id})
            credential = (await session.execute(text("SELECT provider_id,issuer,account_id,version FROM credentials "
                "WHERE id=:id FOR UPDATE"), {"id": identity})).mappings().one()
            provider = ProviderConfig.model_validate(config)
            if (credential["provider_id"] != provider_id or credential["issuer"] != ISSUER or not credential["account_id"]
                    or provider.adapter != "codex_oauth" or provider.auth_mode != "oauth"):
                raise GatewayError("invalid_state", 409)
            if credential["version"] != payload.version:
                raise GatewayError("version_conflict", 409)
            if await session.scalar(text("SELECT EXISTS(SELECT 1 FROM codex_reset_requests WHERE credential_id=:id "
                    "AND state IN ('prepared','dispatched','unknown','succeeded_refresh_failed'))"), {"id": identity}):
                raise GatewayError("operation_conflict", 409)
            # Cooperates with delete_proxy's pre-check UPDATE lock so an
            # uncommitted reference cannot race deletion (profile_id has no FK).
            if payload.proxy_profile_id is not None and not await session.scalar(text(
                    "SELECT id FROM proxy_profiles WHERE id=:id FOR KEY SHARE"), {"id": payload.proxy_profile_id}):
                raise GatewayError("invalid_request", 422)
            await session.execute(text("UPDATE credentials SET enabled=:enabled,profile_id=:profile,version=version+1 WHERE id=:id"),
                {"id": identity, "enabled": payload.enabled, "profile": payload.proxy_profile_id})
            await self.services.identity._audit(session, "codex.account.updated", identity,
                {"version": payload.version + 1, "enabled": payload.enabled,
                 "proxy_profile_id": str(payload.proxy_profile_id) if payload.proxy_profile_id else None})
        return {"id": identity, "version": payload.version + 1}


def mount_codex_routes(router, services):
    views, reports = AccountViews(services), UsageReports(services.db)

    @router.get("/codex-service/status")
    async def service_status():
        return await ServiceStatus(services).read()

    @router.get("/codex-service/proxy")
    async def service_proxy():
        return await CodexProxy(services).read()

    @router.put("/codex-service/proxy")
    async def update_service_proxy(payload: CodexProxyInput):
        return await CodexProxy(services).update(payload)

    @router.get("/oauth-accounts")
    async def accounts(limit: int = Query(100, ge=1, le=200), after: UUID | None = None):
        return await views.list(limit, after)

    @router.patch("/oauth-accounts/{identity}")
    async def update(identity: UUID, payload: AccountUpdate):
        return await views.update(identity, payload)

    @router.get("/oauth-accounts/{identity}/quota")
    async def quota(identity: UUID):
        return await views.projection(identity, "usage")

    @router.get("/oauth-accounts/{identity}/reset-credits")
    async def credits(identity: UUID):
        return await views.projection(identity, "credits")

    @router.post("/oauth-accounts/{identity}/quota/refresh")
    async def refresh(identity: UUID, payload: RefreshInput):
        pending = await services.quota_store.pending_refresh_reset(identity)
        try:
            await services.codex_quota.refresh(identity, datetime.now(timezone.utc) + timedelta(seconds=30))
        except GatewayError as exc:
            # The reviewed service persists both independent fetch outcomes
            # before raising for missing usage. Return that partial truth.
            if exc.code != "codex_usage_unavailable":
                raise
        usage, credits = await views.projection(identity, "usage"), await views.projection(identity, "credits")
        if pending and all(p["snapshot"] is not None and not p["stale"] and not p["last_error"] for p in (usage, credits)):
            try:
                operation_id, generation, status = pending
                await services.quota_store.finish_reset(operation_id, generation, "succeeded", None, status)
            except GatewayError as exc:
                # Another refresh/resolution or changed stamp won the fence.
                # Never weaken it, retry a consume, or resolve an unknown here.
                if exc.code not in {"claim_lost", "invalid_state"}:
                    raise
            credits = await views.projection(identity, "credits")
        return {"usage_status": "failed" if usage["last_error"] else "updated",
                "credits_status": "failed" if credits["last_error"] else "updated",
                "usage": usage, "credits": credits}

    @router.post("/oauth-accounts/{identity}/reset-credits/consume")
    async def consume(identity: UUID, payload: ResetConsumeInput, response: Response):
        result = await services.codex_reset.consume(identity, payload.request_id, payload.credits_version,
            payload.acknowledge, "admin", datetime.now(timezone.utc) + timedelta(seconds=30))
        response.status_code = 202 if result.state in {"prepared", "dispatched", "unknown"} else 200
        return result.model_dump(mode="json")

    @router.post("/reset-requests/{identity}/resolve")
    async def resolve(identity: UUID, payload: ResetResolveInput):
        return (await services.quota_store.resolve_reset(identity, payload.version, payload.outcome,
                                                        payload.reason, "admin")).model_dump(mode="json")

    @router.post("/keys/{identity}/quota-adjust")
    async def adjust(identity: UUID, payload: QuotaAdjustInput):
        return await services.adjustments.grant(identity, payload.request_id, payload.version, payload.amount_micro, payload.reason)

    @router.get("/account-pools/{model_id:path}")
    async def pool_get(model_id: str):
        value = await services.pool_store.get(model_id)
        return {"model_id": model_id, "version": value[0] if value else 0,
                "policy": value[1].model_dump(mode="json") if value else None}

    @router.put("/account-pools/{model_id:path}")
    async def pool_put(model_id: str, payload: PoolInput):
        version = await services.pool_store.put(model_id, payload.version or None, payload.policy, "admin")
        return {"model_id": model_id, "version": version, "policy": payload.policy.model_dump(mode="json")}

    @router.get("/capabilities")
    async def capabilities():
        from ..providers.codex_model_catalog import codex_model_catalog
        return {"codex_oauth": codex_runtime_capabilities(), "model_catalog": codex_model_catalog(),
                "openai_compatible": {"wire_apis": ["chat", "responses"], "text": True, "tools": True},
                "anthropic": {"wire_apis": ["messages"], "text": True, "tools": True},
                "gemini": {"wire_apis": ["generateContent"], "text": True, "tools": True},
                "ollama": {"wire_apis": ["chat"], "text": True, "tools": True}}

    from .codex_models import mount_codex_models
    mount_codex_models(router, services)

    @router.get("/usage/requests")
    async def requests(credential_id: UUID | None = None, model_id: str | None = Query(None, min_length=1, max_length=200),
                       from_: datetime | None = Query(None, alias="from"), to: datetime | None = None,
                       limit: int = Query(100, ge=1, le=200), after: UUID | None = None):
        start, end = report_window(from_, to)
        return await reports.report_requests(credential_id, model_id, start, end, limit, after)

    @router.get("/usage/accounts")
    async def accounts_report(credential_id: UUID | None = None, model_id: str | None = Query(None, min_length=1, max_length=200),
                              from_: datetime | None = Query(None, alias="from"), to: datetime | None = None,
                              limit: int = Query(100, ge=1, le=200), after: UUID | None = None):
        start, end = report_window(from_, to)
        return await reports.report_accounts(credential_id, model_id, start, end, limit, after)
