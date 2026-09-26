from datetime import timedelta
from math import ceil
import json
import time
from uuid import UUID, uuid4

from sqlalchemy import text

from ..errors import GatewayError
from ..identity.policy import KeyPolicy
from ..metering.records import Bounds
from ..routing.records import ModelConfig, ProviderConfig, RouteSnapshot


def json_value(value):
    def encode(item):
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, (set, frozenset)):
            return sorted(item)
        raise TypeError()
    return json.dumps(value, default=encode)


class Catalog:
    def __init__(self, db, vault):
        self.db, self.vault = db, vault

    async def _audit(self, session, action, identity):
        await session.execute(text("INSERT INTO audit_events(id,actor,action,record_id) VALUES (:id,'admin',:action,:record)"),
                              {"id": uuid4(), "action": action, "record": identity})

    async def create_provider(self, config: ProviderConfig):
        identity = uuid4()
        payload = config.model_dump_json()
        async with self.db.sessions.begin() as session:
            await session.execute(text("INSERT INTO providers(id,config) VALUES (:id,CAST(:config AS jsonb))"),
                                  {"id": identity, "config": payload})
            await session.execute(text("INSERT INTO config_versions(provider_id,version,config) "
                                       "VALUES (:id,1,CAST(:config AS jsonb))"), {"id": identity, "config": payload})
            await self._audit(session, "provider.created", identity)
        return identity

    async def update_provider(self, provider_id, version, config: ProviderConfig):
        async with self.db.sessions.begin() as session:
            updated = await session.scalar(text("UPDATE providers SET config=CAST(:config AS jsonb),version=version+1 "
                "WHERE id=:id AND version=:version RETURNING version"),
                {"id": provider_id, "version": version, "config": config.model_dump_json()})
            if updated is None:
                raise GatewayError("version_conflict", 409)
            await session.execute(text("INSERT INTO config_versions(provider_id,version,config) "
                "VALUES (:id,:version,CAST(:config AS jsonb))"),
                {"id": provider_id, "version": updated, "config": config.model_dump_json()})
            await self._audit(session, "provider.updated", provider_id)

    async def put_credential(self, provider_id, secret):
        if not isinstance(secret, str) or not 1 <= len(secret) <= 65536:
            raise GatewayError("invalid_request")
        identity = uuid4()
        encrypted = self.vault.seal("credential", identity, secret.encode()).to_dict()
        async with self.db.sessions.begin() as session:
            exists = await session.scalar(text("SELECT id FROM providers WHERE id=:id"), {"id": provider_id})
            if not exists:
                raise GatewayError("not_found", 404)
            await session.execute(text("INSERT INTO credentials(id,provider_id,encrypted_secret) "
                "VALUES (:id,:provider,CAST(:secret AS jsonb))"),
                {"id": identity, "provider": provider_id, "secret": json_value(encrypted)})
            await self._audit(session, "credential.created", identity)
        return identity

    async def set_credential_enabled(self, credential_id, enabled):
        if type(enabled) is not bool:
            raise GatewayError("invalid_request")
        async with self.db.sessions.begin() as session:
            identity = await session.scalar(text("UPDATE credentials SET enabled=:enabled WHERE id=:id RETURNING id"),
                                           {"id": credential_id, "enabled": enabled})
            if not identity:
                raise GatewayError("not_found", 404)
            await self._audit(session, "credential.enabled" if enabled else "credential.disabled", identity)

    async def rotate_credential(self, credential_id, secret, *, expected_version=None):
        if not isinstance(secret, str) or not 1 <= len(secret) <= 65536:
            raise GatewayError("invalid_request")
        cipher = self.vault.seal("credential", credential_id, secret.encode()).to_dict()
        async with self.db.sessions.begin() as session:
            if expected_version is not None:
                current=await session.scalar(text('SELECT version FROM credentials WHERE id=:id FOR UPDATE'),{'id':credential_id})
                if current!=expected_version:
                    raise GatewayError('version_conflict',409)
            result = await session.scalar(text("UPDATE credentials SET encrypted_secret=CAST(:secret AS jsonb),version=version+1 "
                "WHERE id=:id RETURNING id"), {"id": credential_id, "secret": json_value(cipher)})
            if not result:
                raise GatewayError("not_found", 404)
            await self._audit(session, "credential.rotated", credential_id)

    async def record_discovery(self, provider_id, model_ids):
        if len(model_ids) > 10000 or any(not isinstance(m, str) or not 1 <= len(m) <= 200 for m in model_ids):
            raise GatewayError("invalid_request")
        async with self.db.sessions.begin() as session:
            for model in sorted(set(model_ids)):
                await session.execute(text("INSERT INTO discovered_models(provider_id,model_id) VALUES (:id,:model) "
                                           "ON CONFLICT DO NOTHING"), {"id": provider_id, "model": model})

    async def discovered_models(self, provider_id):
        async with self.db.sessions() as session:
            return list((await session.execute(text("SELECT model_id FROM discovered_models WHERE provider_id=:id "
                                                    "ORDER BY model_id"), {"id": provider_id})).scalars())

    async def put_model(self, model: ModelConfig):
        from .registry_writes import lock_model_names, write_model
        async with self.db.sessions.begin() as session:
            # Legacy callers use this method as a replace operation; it remains
            # serialized and compare-and-set under the shared model-name lock.
            await lock_model_names(session, (model.model_id,))
            current = await session.scalar(text("SELECT version FROM public_models WHERE id=:id FOR UPDATE"),
                                           {"id": model.model_id})
            await write_model(session, model, expected_version=current, create_only=current is None)

    async def put_alias(self, alias, model_id):
        if (not isinstance(alias, str) or not 1 <= len(alias) <= 200 or alias == model_id
                or any(ord(c) < 33 or ord(c) > 126 for c in alias)
                or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./:-" for c in alias)
                or "*" in alias or "?" in alias or "//" in alias
                or any(part in {".", "..", ""} for part in alias.split("/"))):
            raise GatewayError("invalid_request")
        async with self.db.sessions.begin() as session:
            from .registry_writes import lock_model_names
            await lock_model_names(session, (alias, model_id))
            conflict = await session.scalar(text("SELECT id FROM public_models WHERE id=:id"), {"id": alias})
            target = await session.scalar(text("SELECT id FROM public_models WHERE id=:id"), {"id": model_id})
            if conflict or not target:
                raise GatewayError("invalid_request")
            await session.execute(text("INSERT INTO model_aliases(alias,model_id) VALUES (:alias,:model) "
                "ON CONFLICT(alias) DO UPDATE SET model_id=excluded.model_id"), {"alias": alias, "model": model_id})

    async def put_binding(self, binding, *, expected_version=None, create_only=False):
        from .registry_writes import lock_model_names, write_binding
        async with self.db.sessions.begin() as session:
            # The publisher holds provider -> credential before taking a model
            # advisory lock. Match that order before the binding FK can wait.
            provider = await session.scalar(text("SELECT id FROM providers WHERE id=:id FOR UPDATE"),
                                            {"id": binding.provider_id})
            credential = await session.scalar(text("SELECT provider_id FROM credentials WHERE id=:id FOR UPDATE"),
                                              {"id": binding.credential_id})
            if provider is None or credential != binding.provider_id:
                raise GatewayError("invalid_request")
            previous_model = await session.scalar(text("SELECT model_id FROM model_bindings WHERE id=:id"),
                                                  {"id": binding.id})
            await lock_model_names(session, (binding.public_model_id, previous_model) if previous_model else
                                   (binding.public_model_id,))
            existing = await session.scalar(text('SELECT version FROM model_bindings WHERE id=:id FOR UPDATE'), {'id': binding.id})
            if existing is not None and expected_version is None and not create_only:
                expected_version = existing
            await write_binding(session, binding, expected_version=expected_version, create_only=create_only)
            await self._audit(session,'binding.updated' if existing else 'binding.created',binding.id)

    async def _policy(self, session, principal):
        row = (await session.execute(text("""SELECT k.policy,k.version FROM api_keys k JOIN customers c ON c.id=k.customer_id
            WHERE k.id=:id AND k.customer_id=:owner AND k.enabled AND c.enabled AND k.revoked_at IS NULL
            AND (k.expires_at IS NULL OR k.expires_at>now())"""),
            {"id": principal.key_id, "owner": principal.customer_id})).mappings().first()
        if not row or row["version"] != principal.policy_version:
            raise GatewayError("invalid_api_key", 401, "auth")
        return KeyPolicy.model_validate(row["policy"])

    async def list_models(self, principal):
        async with self.db.sessions() as session:
            policy = await self._policy(session, principal)
            configs = (await session.execute(text("SELECT config FROM public_models ORDER BY id"))).scalars().all()
            models = [ModelConfig.model_validate(config) for config in configs]
            return [m for m in models if m.enabled and self._allowed_model(policy, m.model_id)]

    @staticmethod
    def _allowed_model(policy, model_id):
        return policy.allows_model(model_id)

    async def list_model_names(self, principal):
        """Wire-only projection; stored and routing ModelConfig identities stay canonical."""
        async with self.db.sessions() as session:
            policy = await self._policy(session, principal)
            configs = (await session.execute(text("SELECT config FROM public_models ORDER BY id"))).scalars()
            models = (ModelConfig.model_validate(config) for config in configs)
            return [policy.model_prefix + m.model_id for m in models
                    if m.enabled and self._allowed_model(policy, m.model_id)]

    async def visible_model(self, principal, canonical_model):
        async with self.db.sessions() as session:
            policy = await self._policy(session, principal)
            if not self._allowed_model(policy, canonical_model):
                raise GatewayError("permission_denied", 403, "policy")
            return policy.model_prefix + canonical_model

    async def _normalize_model(self, session, policy, requested_model):
        token = requested_model
        if policy.model_prefix:
            if not isinstance(token, str) or not token.startswith(policy.model_prefix):
                raise GatewayError("permission_denied", 403, "policy")
            token = token[len(policy.model_prefix):]
            if not token:
                raise GatewayError("permission_denied", 403, "policy")
        alias = await session.scalar(text("SELECT model_id FROM model_aliases WHERE alias=:alias"), {"alias": token})
        model_id = alias or token
        if not self._allowed_model(policy, model_id):
            raise GatewayError("permission_denied", 403, "policy")
        if not await session.scalar(text("SELECT id FROM public_models WHERE id=:id"), {"id": model_id}):
            raise GatewayError("not_found", 404)
        return model_id

    async def _model_row(self, session, principal, model_id):
        """Authorize a canonical ID; never strip prefixes or resolve aliases here."""
        policy = await self._policy(session, principal)
        if not self._allowed_model(policy, model_id):
            raise GatewayError("permission_denied", 403, "policy")
        model_row = (await session.execute(text("SELECT * FROM public_models WHERE id=:id FOR UPDATE"),
                                           {"id": model_id})).mappings().first()
        if model_row is None:
            raise GatewayError("upstream_unavailable", 503)
        model = ModelConfig.model_validate(model_row["config"])
        if not model.enabled:
            raise GatewayError("upstream_unavailable", 503)
        return model_row, model

    async def resolve_model(self, principal, requested_model):
        """Resolve authorized model identity before looking up a continuation."""
        async with self.db.sessions.begin() as session:
            policy = await self._policy(session, principal)
            canonical_model = await self._normalize_model(session, policy, requested_model)
            row, _ = await self._model_row(session, principal, canonical_model)
            return row["id"]

    async def _candidate_state(self, session, principal, request, *, binding_id=None):
        model_row, model = await self._model_row(session, principal, request.model)
        model_id = model_row["id"]
        rows = (await session.execute(text("""SELECT b.id AS binding_id,b.config AS binding,p.id AS provider_id,
            p.config AS provider,p.version,c.id AS credential_id,c.profile_id AS credential_profile,
            GREATEST(p.cooldown_until,c.cooldown_until) AS cooldown_until FROM model_bindings b
            JOIN providers p ON p.id=b.provider_id JOIN credentials c ON c.id=b.credential_id
            WHERE b.model_id=:model AND c.enabled AND c.health='active' ORDER BY b.id"""),
            {"model": model_id})).mappings().all()
        routes, mismatch, cooldowns = [], False, []
        now = await session.scalar(text("SELECT clock_timestamp()"))
        for row in rows:
            if binding_id is not None and row["binding_id"] != binding_id:
                continue
            binding = row["binding"]
            provider = ProviderConfig.model_validate(row["provider"])
            if (not provider.enabled or not binding["enabled"]
                    or not model.router_model and binding["identity"] != model.identity):
                continue
            caps = frozenset(binding["capabilities"])
            # Compact and Responses WebSocket are transport capabilities of the
            # Codex adapter, unlike vision/images which remain account/binding
            # specific and must be explicitly granted by the admin.
            if provider.adapter == "codex_oauth":
                caps = caps | {"compact", "websocket"}
            if not request.required_capabilities <= caps:
                mismatch = True
                continue
            if row["cooldown_until"] is not None and row["cooldown_until"] > now:
                cooldowns.append(row["cooldown_until"])
                continue
            routes.append(RouteSnapshot(row["binding_id"], row["provider_id"], row["credential_id"], model_id,
                binding["upstream_model"], provider.adapter, provider.root, provider.auth_mode, row["version"], caps,
                Bounds(binding["input_bound"], binding["output_bound"]), row["credential_profile"] or provider.proxy_profile_id, provider.budget_id,
                binding["priority"], model.input_micro, model.output_micro, provider.timeout, provider.cost_schedule, provider.wire_api,
                cache_read_micro=model.cache_read_micro, cache_write_micro=model.cache_write_micro))
        if not routes and not cooldowns:
            raise GatewayError("unsupported_feature" if mismatch else "upstream_unavailable", 400 if mismatch else 503)
        retry_after = min(86400, max(0, ceil((min(cooldowns) - now).total_seconds()))) if cooldowns else 0
        return model_row, routes, retry_after

    async def retry_after(self, principal, request, *, binding_id=None):
        """Read current eligibility without dispatching or advancing round-robin."""
        async with self.db.sessions.begin() as session:
            _, routes, delay = await self._candidate_state(session, principal, request, binding_id=binding_id)
            # An eligible route means retry now; otherwise use the earliest
            # locally stored cooldown deadline.
            return 0 if routes else delay

    async def candidates(self, principal, request, *, binding_id=None, canonical_model=False):
        if not canonical_model:
            from types import SimpleNamespace
            request = SimpleNamespace(model=await self.resolve_model(principal, request.model),
                                      required_capabilities=request.required_capabilities)
        async with self.db.sessions.begin() as session:
            model_row, routes, retry_after = await self._candidate_state(session, principal, request,
                                                                          binding_id=binding_id)
            if not routes:
                raise GatewayError("rate_limited", 429, "upstream", retry_after)
            priority = min(route.priority for route in routes)
            first = [route for route in routes if route.priority == priority]
            rest = sorted((route for route in routes if route.priority != priority), key=lambda route: route.priority)
            offset = model_row["cursor"] % len(first)
            await session.execute(text("UPDATE public_models SET cursor=(cursor+1)%2147483647 WHERE id=:id"), {"id": model_row["id"]})
            return first[offset:] + first[:offset] + rest

    async def account_candidates(self, principal, request, *, binding_id=None, canonical_model=False):
        """Enrich authorized routes without changing their legacy order or policy.

        Metadata is an observation, not an authorization lease. Consumers still
        revalidate the chosen credential and binding before network dispatch.
        """
        routes = await self.candidates(principal, request, binding_id=binding_id, canonical_model=canonical_model)
        return await self._account_observations(routes)

    async def _account_observations(self, routes):
        from ..accounts.quota_records import CodexQuotaSnapshot
        from ..routing.pool_records import AccountCandidate
        result = []
        async with self.db.sessions() as session:
            for route in routes:
                row = (await session.execute(text("""SELECT c.health,
                    GREATEST(c.cooldown_until,p.cooldown_until) AS cooldown_until,
                    a.usage_snapshot,a.usage_error,a.subscription_expires_at
                    FROM credentials c JOIN providers p ON p.id=c.provider_id
                    LEFT JOIN codex_account_state a ON a.credential_id=c.id WHERE c.id=:id"""),
                    {"id": route.credential_id})).mappings().first()
                if row is None:
                    continue
                quota = (CodexQuotaSnapshot.model_validate_json(json.dumps(row["usage_snapshot"]))
                         if row["usage_snapshot"] is not None else None)
                if quota is not None and row["usage_error"] is not None:
                    quota = quota.model_copy(update={"last_error": "codex_usage_unavailable"})
                result.append(AccountCandidate(route=route, quota=quota, health=row["health"],
                    cooldown_until=row["cooldown_until"], subscription_expires_at=row["subscription_expires_at"]))
        return tuple(result)

    async def pool_policy(self, model_id):
        from ..routing.pool_records import AccountPoolPolicy
        async with self.db.sessions() as session:
            value = await session.scalar(text("SELECT policy FROM account_pool_policies WHERE model_id=:id"),
                                         {"id": model_id})
        return AccountPoolPolicy.model_validate_json(json.dumps(value)) if value is not None else None

    async def revalidate(self, principal, request, route):
        """Live authorization without advancing the legacy round-robin cursor."""
        async with self.db.sessions.begin() as session:
            _, routes, retry_after = await self._candidate_state(session, principal, request, binding_id=route.binding_id)
        if not routes:
            raise GatewayError("rate_limited", 429, "upstream", retry_after)
        if routes[0].credential_id != route.credential_id:
            raise GatewayError("invalid_state")
        return routes[0]

    async def cooldown(self, provider_id, credential_id, seconds, *, started_at=None):
        if type(seconds) is not int or not 0 <= seconds <= 86400:
            raise GatewayError("invalid_request")
        async with self.db.sessions.begin() as session:
            now = await session.scalar(text("SELECT clock_timestamp()"))
            # Retry-After starts at receipt, not after cleanup or database waits.
            remaining = seconds if started_at is None else max(0, seconds - (time.monotonic() - started_at))
            if credential_id is None:
                await session.execute(text("UPDATE providers SET cooldown_until=GREATEST(cooldown_until,:until) WHERE id=:id"),
                                      {"until": now + timedelta(seconds=remaining), "id": provider_id})
            else:
                await session.execute(text("UPDATE credentials SET cooldown_until=GREATEST(cooldown_until,:until) WHERE id=:id AND provider_id=:provider"),
                                      {"until": now + timedelta(seconds=remaining), "id": credential_id, "provider": provider_id})

    async def credential_outcome(self, provider_id, credential_id, error=None):
        """Observation only: transport success never repairs OAuth health."""
        from ..accounts.quota_storage import safe_code
        safe_code(error)
        async with self.db.sessions.begin() as session:
            await session.execute(text("SELECT id FROM providers WHERE id=:id FOR KEY SHARE"), {"id": provider_id})
            identity = await session.scalar(text("SELECT id FROM credentials WHERE id=:id AND provider_id=:provider "
                "FOR KEY SHARE"), {"id": credential_id, "provider": provider_id})
            if identity is None:
                raise GatewayError("invalid_state")
            await session.execute(text("INSERT INTO codex_account_state(credential_id) VALUES (:id) ON CONFLICT DO NOTHING"),
                                  {"id": identity})
            await session.execute(text("UPDATE codex_account_state SET last_attempt_at=clock_timestamp(),"
                "last_success_at=CASE WHEN CAST(:error AS text) IS NULL THEN clock_timestamp() ELSE last_success_at END,"
                "last_failure_at=CASE WHEN CAST(:error AS text) IS NOT NULL THEN clock_timestamp() ELSE last_failure_at END,"
                "last_error=CAST(:error AS text),failure_count=CASE WHEN CAST(:error AS text) IS NULL THEN 0 ELSE LEAST(failure_count+1,2147483647) END "
                "WHERE credential_id=:id"), {"id": identity, "error": error})
