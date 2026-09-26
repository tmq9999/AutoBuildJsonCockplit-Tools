"""Explicit, additive publication of the Codex reference catalog."""
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from ..accounts.imports import CODEX_PROVIDER
from ..errors import GatewayError
from ..providers.codex_model_catalog import codex_model_catalog
from ..providers.registry_writes import lock_model_names
from ..providers.catalog import json_value
from ..routing.records import ModelConfig


class PublishCodexModels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_ids: list[str] = Field(min_length=1, max_length=32)
    image_credential_ids: list[UUID] = Field(default_factory=list, max_length=10000)
    # Operator-defined reservation bounds, not claims about the upstream model.
    input_bound: int = Field(default=1_000_000, strict=True, ge=1, le=2_000_000)
    output_bound: int = Field(default=128000, strict=True, ge=1, le=1_000_000)


async def publish_codex_models(services, payload):
    available = {m["id"]: m for m in codex_model_catalog()}
    chosen = sorted(set(payload.model_ids))
    if not set(chosen) <= set(available):
        raise GatewayError("invalid_request")
    async with services.db.sessions.begin() as session:
        provider = await session.scalar(text("SELECT config FROM providers WHERE id=:id FOR UPDATE"), {"id": CODEX_PROVIDER})
        if not provider or provider.get("adapter") != "codex_oauth":
            raise GatewayError("not_found", 404)
        credentials = list((await session.execute(text("SELECT id FROM credentials WHERE provider_id=:id "
            "AND enabled ORDER BY id FOR UPDATE"), {"id": CODEX_PROVIDER})).scalars())
        if not credentials:
            raise GatewayError("upstream_unavailable", 503)
        images = set(payload.image_credential_ids)
        if not images <= set(credentials):
            raise GatewayError("invalid_request")
        if any("images" in available[m]["capabilities"] for m in chosen) and not images:
            raise GatewayError("image_accounts_required", 400)
        await lock_model_names(session, chosen)
        models_created, bindings_created = 0, 0
        for model in chosen:
            if await session.scalar(text("SELECT 1 FROM model_aliases WHERE alias=:id"), {"id": model}):
                raise GatewayError("version_conflict", 409)
            config = ModelConfig(model_id=model, identity=model, enabled=True)
            created = await session.scalar(text("INSERT INTO public_models(id,config) VALUES (:id,CAST(:config AS jsonb)) "
                "ON CONFLICT DO NOTHING RETURNING id"), {"id": model, "config": config.model_dump_json()})
            models_created += int(created is not None)
            current = await session.scalar(text("SELECT config FROM public_models WHERE id=:id"), {"id": model})
            if current["identity"] != model:
                raise GatewayError("version_conflict", 409)
            item = available[model]
            for credential in credentials:
                if "images" in item["capabilities"] and credential not in images:
                    continue
                # Preserve all existing manual mapping, enabled states and rates.
                exists = await session.scalar(text("SELECT 1 FROM model_bindings WHERE provider_id=:provider "
                    "AND credential_id=:credential AND model_id=:model LIMIT 1"),
                    {"provider": CODEX_PROVIDER, "credential": credential, "model": model})
                if exists:
                    continue
                identifier = uuid5(CODEX_PROVIDER, str(credential)+":"+model)
                caps = set(item["capabilities"])
                if credential in images and "vision" in caps:
                    caps.add("images")
                binding = {"id": identifier, "provider_id": CODEX_PROVIDER, "credential_id": credential,
                    "public_model_id": model, "upstream_model": model, "identity": model,
                    "input_bound": payload.input_bound, "output_bound": payload.output_bound,
                    "capabilities": caps, "priority": 0, "enabled": True}
                await session.execute(text("INSERT INTO model_bindings(id,provider_id,credential_id,model_id,config) "
                    "VALUES (:id,:provider,:credential,:model,CAST(:config AS jsonb))"),
                    {"id": identifier, "provider": CODEX_PROVIDER, "credential": credential,
                     "model": model, "config": json_value(binding)})
                bindings_created += 1
        await session.execute(text("INSERT INTO audit_events(id,actor,action,record_id) "
            "VALUES (:id,'admin','codex.models.published',:record)"), {"id": uuid4(), "record": CODEX_PROVIDER})
        return {"models_created": models_created, "bindings_created": bindings_created,
                "accounts": len(credentials), "image_accounts": len(images)}


def mount_codex_models(router, services):
    @router.get('/codex-models')
    async def catalog():
        return {"items": codex_model_catalog(), "source": "reference_catalog", "account_access_verified": False}

    @router.post('/codex-models/publish')
    async def publish(payload: PublishCodexModels):
        return await publish_codex_models(services, payload)
