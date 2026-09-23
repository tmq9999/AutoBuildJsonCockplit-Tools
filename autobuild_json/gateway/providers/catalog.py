from dataclasses import asdict
from datetime import timedelta
import json
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
        async with self.db.sessions.begin() as session:
            exists = await session.scalar(text("SELECT alias FROM model_aliases WHERE alias=:id"), {"id": model.model_id})
            if exists:
                raise GatewayError("invalid_request")
            await session.execute(text("INSERT INTO public_models(id,config) VALUES (:id,CAST(:config AS jsonb)) "
                "ON CONFLICT(id) DO UPDATE SET config=excluded.config"),
                {"id": model.model_id, "config": model.model_dump_json()})

    async def put_alias(self, alias, model_id):
        if not isinstance(alias, str) or not 1 <= len(alias) <= 200 or alias == model_id:
            raise GatewayError("invalid_request")
        async with self.db.sessions.begin() as session:
            conflict = await session.scalar(text("SELECT id FROM public_models WHERE id=:id"), {"id": alias})
            target = await session.scalar(text("SELECT id FROM public_models WHERE id=:id"), {"id": model_id})
            if conflict or not target:
                raise GatewayError("invalid_request")
            await session.execute(text("INSERT INTO model_aliases(alias,model_id) VALUES (:alias,:model) "
                "ON CONFLICT(alias) DO UPDATE SET model_id=excluded.model_id"), {"alias": alias, "model": model_id})

    async def put_binding(self, binding, *, expected_version=None, create_only=False):
        async with self.db.sessions.begin() as session:
            existing=await session.scalar(text('SELECT version FROM model_bindings WHERE id=:id FOR UPDATE'),{'id':binding.id})
            if create_only and existing is not None or expected_version is not None and existing!=expected_version:
                raise GatewayError('version_conflict',409)
            provider = await session.scalar(text("SELECT provider_id FROM credentials WHERE id=:id"), {"id": binding.credential_id})
            config = await session.scalar(text("SELECT config FROM public_models WHERE id=:id"), {"id": binding.public_model_id})
            if provider != binding.provider_id or config is None:
                raise GatewayError("invalid_request")
            model = ModelConfig.model_validate(config)
            if not model.router_model and model.identity != binding.identity:
                raise GatewayError("invalid_request")
            await session.execute(text("""INSERT INTO model_bindings(id,provider_id,credential_id,model_id,config)
                VALUES (:id,:provider,:credential,:model,CAST(:config AS jsonb)) ON CONFLICT(id) DO UPDATE
                SET provider_id=excluded.provider_id,credential_id=excluded.credential_id,
                    model_id=excluded.model_id,config=excluded.config,version=model_bindings.version+1"""),
                {"id": binding.id, "provider": binding.provider_id, "credential": binding.credential_id,
                 "model": binding.public_model_id, "config": json_value(asdict(binding))})
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
            return [m for m in models if m.enabled and (policy.all_models or m.model_id in policy.model_ids)]

    async def candidates(self, principal, request):
        async with self.db.sessions.begin() as session:
            policy = await self._policy(session, principal)
            alias = await session.scalar(text("SELECT model_id FROM model_aliases WHERE alias=:alias"), {"alias": request.model})
            model_id = alias or request.model
            if not policy.all_models and model_id not in policy.model_ids:
                raise GatewayError("permission_denied", 403, "policy")
            model_row = (await session.execute(text("SELECT * FROM public_models WHERE id=:id FOR UPDATE"),
                                               {"id": model_id})).mappings().first()
            if model_row is None:
                raise GatewayError("upstream_unavailable", 503)
            model = ModelConfig.model_validate(model_row["config"])
            if not model.enabled:
                raise GatewayError("upstream_unavailable", 503)
            rows = (await session.execute(text("""SELECT b.id AS binding_id,b.config AS binding,p.id AS provider_id,
                p.config AS provider,p.version,c.id AS credential_id,c.profile_id AS credential_profile FROM model_bindings b
                JOIN providers p ON p.id=b.provider_id JOIN credentials c ON c.id=b.credential_id
                WHERE b.model_id=:model AND c.enabled AND c.health='active'
                AND (p.cooldown_until IS NULL OR p.cooldown_until <= now())
                AND (c.cooldown_until IS NULL OR c.cooldown_until <= now()) ORDER BY b.id"""),
                {"model": model_id})).mappings().all()
            routes, mismatch = [], False
            for row in rows:
                binding = row["binding"]
                provider = ProviderConfig.model_validate(row["provider"])
                if (not provider.enabled or not binding["enabled"]
                        or not model.router_model and binding["identity"] != model.identity):
                    continue
                caps = frozenset(binding["capabilities"])
                if not request.required_capabilities <= caps:
                    mismatch = True
                    continue
                routes.append(RouteSnapshot(row["binding_id"], row["provider_id"], row["credential_id"], model_id,
                    binding["upstream_model"], provider.adapter, provider.root, provider.auth_mode, row["version"], caps,
                    Bounds(binding["input_bound"], binding["output_bound"]), row["credential_profile"] or provider.proxy_profile_id, provider.budget_id,
                    binding["priority"], model.input_micro, model.output_micro, provider.timeout, provider.cost_schedule, provider.wire_api))
            if not routes:
                raise GatewayError("unsupported_feature" if mismatch else "upstream_unavailable", 400 if mismatch else 503)
            priority = min(route.priority for route in routes)
            first = [route for route in routes if route.priority == priority]
            rest = sorted((route for route in routes if route.priority != priority), key=lambda route: route.priority)
            offset = model_row["cursor"] % len(first)
            await session.execute(text("UPDATE public_models SET cursor=(cursor+1)%2147483647 WHERE id=:id"), {"id": model_id})
            return first[offset:] + first[:offset] + rest

    async def cooldown(self, provider_id, credential_id, seconds):
        if type(seconds) is not int or not 0 <= seconds <= 86400:
            raise GatewayError("invalid_request")
        async with self.db.sessions.begin() as session:
            now = await session.scalar(text("SELECT clock_timestamp()"))
            if credential_id is None:
                await session.execute(text("UPDATE providers SET cooldown_until=:until WHERE id=:id"),
                                      {"until": now + timedelta(seconds=seconds), "id": provider_id})
            else:
                await session.execute(text("UPDATE credentials SET cooldown_until=:until WHERE id=:id AND provider_id=:provider"),
                                      {"until": now + timedelta(seconds=seconds), "id": credential_id, "provider": provider_id})
