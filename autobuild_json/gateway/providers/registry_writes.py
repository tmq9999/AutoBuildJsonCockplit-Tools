"""Compare-and-set registry writes shared by catalog publication and admin CRUD."""

import hashlib

from sqlalchemy import text

from ..errors import GatewayError
from ..routing.records import BindingConfig, ModelConfig


def _lock_key(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big", signed=True)


async def lock_model_names(session, names):
    """Serialize inserts and updates involving model IDs and aliases."""
    for name in sorted({str(value) for value in names}):
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key(name)})


async def write_model(session, model: ModelConfig, *, expected_version: int | None = None,
                      create_only: bool = False) -> int:
    await lock_model_names(session, (model.model_id,))
    current = (await session.execute(text("SELECT version FROM public_models WHERE id=:id FOR UPDATE"),
                                     {"id": model.model_id})).scalar()
    if create_only and current is not None:
        raise GatewayError("version_conflict", 409)
    if expected_version is not None and current != expected_version:
        raise GatewayError("version_conflict", 409)
    alias = await session.scalar(text("SELECT alias FROM model_aliases WHERE alias=:id"), {"id": model.model_id})
    if alias:
        raise GatewayError("invalid_request")
    payload = model.model_dump_json()
    if current is None:
        if expected_version is not None:
            raise GatewayError("version_conflict", 409)
        return await session.scalar(text("INSERT INTO public_models(id,config) VALUES (:id,CAST(:config AS jsonb)) RETURNING version"),
                                    {"id": model.model_id, "config": payload})
    if create_only:
        raise GatewayError("version_conflict", 409)
    return await session.scalar(text("UPDATE public_models SET config=CAST(:config AS jsonb),version=version+1 "
                                    "WHERE id=:id AND version=:version RETURNING version"),
                                {"id": model.model_id, "config": payload, "version": expected_version})


async def write_binding(session, binding: BindingConfig, *, expected_version: int | None = None,
                        create_only: bool = False) -> int:
    await lock_model_names(session, (binding.public_model_id,))
    current = await session.scalar(text("SELECT version FROM model_bindings WHERE id=:id FOR UPDATE"), {"id": binding.id})
    if create_only and current is not None:
        raise GatewayError("version_conflict", 409)
    if expected_version is not None and current != expected_version:
        raise GatewayError("version_conflict", 409)
    provider = await session.scalar(text("SELECT provider_id FROM credentials WHERE id=:id"), {"id": binding.credential_id})
    config = await session.scalar(text("SELECT config FROM public_models WHERE id=:id"), {"id": binding.public_model_id})
    if provider != binding.provider_id or config is None:
        raise GatewayError("invalid_request")
    model = ModelConfig.model_validate(config)
    if not model.router_model and model.identity != binding.identity:
        raise GatewayError("invalid_request")
    payload = __import__("json").dumps({
        "id": str(binding.id), "provider_id": str(binding.provider_id), "credential_id": str(binding.credential_id),
        "public_model_id": binding.public_model_id, "upstream_model": binding.upstream_model,
        "identity": binding.identity, "input_bound": binding.input_bound, "output_bound": binding.output_bound,
        "capabilities": sorted(binding.capabilities), "priority": binding.priority, "enabled": binding.enabled,
    })
    if current is None:
        if expected_version is not None:
            raise GatewayError("version_conflict", 409)
        return await session.scalar(text("""INSERT INTO model_bindings(id,provider_id,credential_id,model_id,config)
            VALUES (:id,:provider,:credential,:model,CAST(:config AS jsonb)) RETURNING version"""),
            {"id": binding.id, "provider": binding.provider_id, "credential": binding.credential_id,
             "model": binding.public_model_id, "config": payload})
    if create_only:
        raise GatewayError("version_conflict", 409)
    return await session.scalar(text("""UPDATE model_bindings SET provider_id=:provider,credential_id=:credential,
            model_id=:model,config=CAST(:config AS jsonb),version=version+1
            WHERE id=:id AND version=:version RETURNING version"""),
        {"id": binding.id, "provider": binding.provider_id, "credential": binding.credential_id,
         "model": binding.public_model_id, "config": payload, "version": expected_version})
