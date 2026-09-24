"""CAS pool policies and finite, tenant-scoped optional session affinity."""
from datetime import datetime, timedelta
import hashlib
import hmac
import json
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text

from ...oauth import ISSUER
from ..accounts.quota_records import CodexQuotaSnapshot
from ..accounts.quota_storage import safe_text
from ..errors import GatewayError
from ..identity.policy import Principal
from ..providers.catalog import Catalog
from ..providers.registry_writes import lock_model_names
from .account_pool import candidate_diagnostic
from .pool_records import AccountCandidate, AccountPoolPolicy, PoolScope
from .records import ModelConfig, ProviderConfig, RouteSnapshot


class PoolStore:
    def __init__(self, db, *, scope_key: bytes):
        if type(scope_key) is not bytes or len(scope_key) < 32:
            raise ValueError("invalid_scope_key")
        self.db, self._scope_key = db, scope_key
        self._catalog = Catalog(db, None)  # Authorization/route reads never open secrets.

    async def _model(self, session, model_id, *, lock=False):
        if not isinstance(model_id, str) or not 1 <= len(model_id) <= 200 or not model_id.isprintable():
            raise GatewayError("invalid_request")
        if lock:
            await lock_model_names(session, (model_id,))
        row = await session.scalar(text("SELECT config FROM public_models WHERE id=:id" +
                                        (" FOR UPDATE" if lock else "")), {"id": model_id})
        if row is None:
            raise GatewayError("invalid_request")
        return ModelConfig.model_validate(row)

    async def get(self, model_id: str) -> tuple[int, AccountPoolPolicy] | None:
        async with self.db.sessions() as session:
            await self._model(session, model_id)
            row = (await session.execute(text("SELECT version,policy FROM account_pool_policies WHERE model_id=:id"),
                                          {"id": model_id})).mappings().first()
            return (row["version"], AccountPoolPolicy.model_validate_json(json.dumps(row["policy"]))) if row else None

    async def put(self, model_id: str, expected_version: int | None, policy: AccountPoolPolicy, actor: str) -> int:
        """None creates only; positive version updates exactly once. Audit is atomic."""
        if expected_version is not None and (type(expected_version) is not int or expected_version < 1):
            raise GatewayError("invalid_request")
        if not isinstance(policy, AccountPoolPolicy):
            raise GatewayError("invalid_request")
        safe_text(actor, 200)
        async with self.db.sessions.begin() as session:
            # Publisher order is provider -> credential -> model. Acquire all
            # provider locks first, then all credentials, in stable UUID order.
            ids = [member.credential_id for member in policy.members]
            providers = (await session.execute(text("SELECT DISTINCT provider_id FROM credentials "
                                                    "WHERE id=ANY(:ids) ORDER BY provider_id"), {"ids": ids})).scalars().all()
            for provider in providers:
                await session.execute(text("SELECT id FROM providers WHERE id=:id FOR UPDATE"), {"id": provider})
            for credential in sorted(ids):
                await session.execute(text("SELECT id FROM credentials WHERE id=:id FOR UPDATE"), {"id": credential})
            model = await self._model(session, model_id, lock=True)
            if not model.enabled:
                raise GatewayError("invalid_request")
            rows = (await session.execute(text("""SELECT b.credential_id,b.config AS binding,
                p.config AS provider,c.enabled,c.health,c.issuer,c.account_id FROM model_bindings b
                JOIN credentials c ON c.id=b.credential_id AND c.provider_id=b.provider_id
                JOIN providers p ON p.id=b.provider_id WHERE b.model_id=:model"""), {"model": model_id})).mappings().all()
            valid = set()
            for row in rows:
                provider = ProviderConfig.model_validate(row["provider"])
                binding = row["binding"]
                if (model.enabled and provider.enabled and provider.adapter == "codex_oauth" and provider.auth_mode == "oauth"
                        and row["enabled"] and row["health"] == "active" and row["issuer"] == ISSUER and row["account_id"]
                        and binding["enabled"] and (model.router_model or binding["identity"] == model.identity)):
                    valid.add(row["credential_id"])
            if ids and (not valid or not set(ids) <= valid):
                raise GatewayError("invalid_request")
            current = await session.scalar(text("SELECT version FROM account_pool_policies WHERE model_id=:id FOR UPDATE"),
                                           {"id": model_id})
            if current != expected_version:
                raise GatewayError("version_conflict", 409)
            version = (current or 0) + 1
            if current is None:
                await session.execute(text("INSERT INTO account_pool_policies(model_id,policy) VALUES (:id,CAST(:policy AS jsonb))"),
                                      {"id": model_id, "policy": policy.model_dump_json()})
            else:
                await session.execute(text("UPDATE account_pool_policies SET version=:version,policy=CAST(:policy AS jsonb) "
                                           "WHERE model_id=:id"),
                                      {"id": model_id, "version": version, "policy": policy.model_dump_json()})
            await session.execute(text("INSERT INTO audit_events(id,actor,action,record_id,details) "
                                       "VALUES (:id,:actor,'codex.pool.updated',:record,CAST(:details AS jsonb))"),
                                  {"id": uuid4(), "actor": actor,
                                   "record": uuid5(UUID("8f2d4d0f-7dd0-4d2f-b315-278c1be2beaf"), "model:" + model_id),
                                   "details": json.dumps({"model_id": model_id, "version": version})})
            return version

    def _digest(self, scope):
        if not isinstance(scope, PoolScope) or scope.session_digest is None:
            raise GatewayError("invalid_state")
        payload = json.dumps([str(scope.customer_id), str(scope.key_id), scope.model_id,
                              scope.session_digest.hex()], separators=(",", ":")).encode()
        return hmac.digest(self._scope_key, b"codex-pool-affinity-v1\x00" + payload, hashlib.sha256)

    async def _scope(self, session, scope):
        # No customer/key row locks: never carry customer quota locks into pool locks.
        version = await session.scalar(text("SELECT version FROM api_keys WHERE id=:id AND customer_id=:owner"),
                                       {"id": scope.key_id, "owner": scope.customer_id})
        if version is None:
            raise GatewayError("invalid_state")
        await self._model(session, scope.model_id, lock=True)
        principal = Principal(scope.customer_id, scope.key_id, version)
        await self._catalog._model_row(session, principal, scope.model_id)
        return principal

    async def _affinity_parent_locks(self, session, scope, route):
        """Acquire affinity FK parents in the publisher's provider->credential order.

        The binding/model/customer/key parents are acquired only after the model
        lock in ``bind``. These are KEY SHARE locks: they protect FK validation
        without taking the update locks used by configuration writers.
        """
        dependency = (await session.execute(text("SELECT provider_id,credential_id,model_id FROM model_bindings "
            "WHERE id=:binding"), {"binding": route.binding_id})).mappings().first()
        if (dependency is None or dependency["model_id"] != scope.model_id
                or dependency["credential_id"] != route.credential_id):
            raise GatewayError("invalid_state")
        await session.execute(text("SELECT id FROM providers WHERE id=:id FOR KEY SHARE"),
                              {"id": dependency["provider_id"]})
        await session.execute(text("SELECT id FROM credentials WHERE id=:id FOR KEY SHARE"),
                              {"id": dependency["credential_id"]})

    async def _live_route(self, session, scope, principal, binding_id, credential_id):
        request = SimpleNamespace(model=scope.model_id, required_capabilities=frozenset())
        _, routes, _ = await self._catalog._candidate_state(session, principal, request, binding_id=binding_id)
        if len(routes) != 1 or routes[0].credential_id != credential_id or routes[0].adapter != "codex_oauth":
            raise GatewayError("invalid_state")
        route = routes[0]
        row = (await session.execute(text("""SELECT c.issuer,c.account_id,a.usage_snapshot,a.usage_error,
            a.subscription_expires_at FROM credentials c LEFT JOIN codex_account_state a ON a.credential_id=c.id
            WHERE c.id=:id"""), {"id": credential_id})).mappings().one()
        if row["issuer"] != ISSUER or not row["account_id"] or route.auth_mode != "oauth":
            raise GatewayError("invalid_state")
        config = await session.scalar(text("SELECT policy FROM account_pool_policies WHERE model_id=:model"),
                                      {"model": scope.model_id})
        if config is not None:
            policy = AccountPoolPolicy.model_validate_json(json.dumps(config))
            quota = (CodexQuotaSnapshot.model_validate_json(json.dumps(row["usage_snapshot"]))
                     if row["usage_snapshot"] is not None else None)
            if quota is not None and row["usage_error"] is not None:
                quota = quota.model_copy(update={"last_error": "codex_usage_unavailable"})
            candidate = AccountCandidate(route=route, health="active", quota=quota,
                                          subscription_expires_at=row["subscription_expires_at"])
            now = await session.scalar(text("SELECT clock_timestamp()"))
            if candidate_diagnostic(policy, candidate, now) is not None:
                raise GatewayError("invalid_state")
        return route

    async def _winner(self, session, scope, principal, row, now):
        if (row["customer_id"], row["key_id"], row["model_id"]) != (scope.customer_id, scope.key_id, scope.model_id):
            raise GatewayError("invalid_state")
        if row["expires_at"] <= now:
            return None
        return await self._live_route(session, scope, principal, row["binding_id"], row["credential_id"])

    async def resolve(self, scope: PoolScope) -> RouteSnapshot | None:
        digest = self._digest(scope)
        try:
            async with self.db.sessions.begin() as session:
                principal = await self._scope(session, scope)
                row = (await session.execute(text("SELECT * FROM account_session_bindings WHERE scope_digest=:digest"),
                                             {"digest": digest})).mappings().first()
                now = await session.scalar(text("SELECT clock_timestamp()"))
                return await self._winner(session, scope, principal, row, now) if row else None
        except GatewayError:
            raise GatewayError("invalid_state") from None

    async def bind(self, scope: PoolScope, route: RouteSnapshot, expires_at: datetime) -> RouteSnapshot:
        """First live writer wins; only DB-expired rows can increment version/rebind."""
        digest = self._digest(scope)
        if (not isinstance(route, RouteSnapshot) or route.public_model_id != scope.model_id
                or not isinstance(expires_at, datetime) or expires_at.tzinfo is None or expires_at.utcoffset() is None):
            raise GatewayError("invalid_state")
        try:
            async with self.db.sessions.begin() as session:
                # Resolve and hold the provider/credential parents before the
                # model lock. Otherwise the affinity INSERT's hidden FK KEY
                # SHARE lock can cycle with put()/put_binding()'s update locks.
                await self._affinity_parent_locks(session, scope, route)
                principal = await self._scope(session, scope)
                # Binding writers acquire model before binding. Customer/key
                # writers acquire customer before key and never acquire pool
                # locks, so affinity takes those parents only after the model.
                await session.execute(text("SELECT id FROM model_bindings WHERE id=:id AND model_id=:model FOR KEY SHARE"),
                                      {"id": route.binding_id, "model": scope.model_id})
                await session.execute(text("SELECT id FROM customers WHERE id=:id FOR KEY SHARE"),
                                      {"id": scope.customer_id})
                await session.execute(text("SELECT id FROM api_keys WHERE id=:id AND customer_id=:customer FOR KEY SHARE"),
                                      {"id": scope.key_id, "customer": scope.customer_id})
                now = await session.scalar(text("SELECT clock_timestamp()"))
                if expires_at <= now:
                    raise GatewayError("invalid_state")
                params = {"digest": digest, "customer": scope.customer_id, "key": scope.key_id, "model": scope.model_id,
                          "binding": route.binding_id, "credential": route.credential_id,
                          "expires": min(expires_at, now + timedelta(hours=24))}
                row = (await session.execute(text("SELECT * FROM account_session_bindings WHERE scope_digest=:digest FOR UPDATE"),
                                             params)).mappings().first()
                if row:
                    winner = await self._winner(session, scope, principal, row, now)
                    if winner is not None:
                        return winner
                live = await self._live_route(session, scope, principal, route.binding_id, route.credential_id)
                if live != route:
                    raise GatewayError("invalid_state")
                await session.execute(text("""INSERT INTO account_session_bindings
                    (scope_digest,customer_id,key_id,model_id,binding_id,credential_id,expires_at)
                    VALUES (:digest,:customer,:key,:model,:binding,:credential,:expires) ON CONFLICT DO NOTHING"""), params)
                await session.execute(text("""UPDATE account_session_bindings SET binding_id=:binding,credential_id=:credential,
                    expires_at=:expires,version=version+1 WHERE scope_digest=:digest AND expires_at<=clock_timestamp()"""), params)
                row = (await session.execute(text("SELECT * FROM account_session_bindings WHERE scope_digest=:digest"),
                                             params)).mappings().one()
                winner = await self._winner(session, scope, principal, row, await session.scalar(text("SELECT clock_timestamp()")))
                if winner is None:
                    raise GatewayError("invalid_state")
                return winner
        except GatewayError:
            raise GatewayError("invalid_state") from None
