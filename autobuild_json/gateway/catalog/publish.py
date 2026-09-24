"""Atomic publication of reviewed provider mappings into the public registry."""

import hashlib
import json
from uuid import uuid4

from sqlalchemy import text

from ..errors import GatewayError
from ..routing.records import BindingConfig, ModelConfig
from ..providers.registry_writes import lock_model_names, write_binding, write_model
from .digests import canonical_bytes
from .records import ConfigStamp, PublicationRequest, PublicationResult, PublishedBinding

_CAPABILITIES = frozenset({"text", "tools", "continuation"})


def _stamp(value):
    try:
        return ConfigStamp.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise GatewayError("invalid_state", 409) from exc


def _same_stamp(actual, expected):
    return all(getattr(actual, field) == getattr(expected, field)
               for field in ("source_version", "provider_version", "credential_version",
                             "proxy_id", "proxy_version", "content_digest"))


class Publisher:
    def __init__(self, db, sources):
        self.db, self.sources = db, sources

    @staticmethod
    def _digest(request: PublicationRequest) -> bytes:
        return hashlib.sha256(canonical_bytes(request.model_dump(mode="json", exclude={"id"}))).digest()

    async def publish(self, request: PublicationRequest, actor) -> PublicationResult:
        if not isinstance(request, PublicationRequest):
            raise GatewayError("invalid_request")
        digest = self._digest(request)
        names = [item.public_model_id for item in request.selections]
        async with self.db.sessions.begin() as session:
            # The receipt lock makes concurrent retries deterministic before any registry mutation.
            await lock_model_names(session, (f"publication:{request.id}",))
            receipt = (await session.execute(text("SELECT payload_digest,result FROM catalog_publications WHERE id=:id FOR UPDATE"),
                                             {"id": request.id})).mappings().first()
            if receipt is not None:
                if bytes(receipt["payload_digest"]) != digest:
                    raise GatewayError("catalog_conflict", 409)
                return PublicationResult.model_validate_json(json.dumps(receipt["result"]))

            context = await self.sources.lock_context(session, request.source_id)
            if context is None or not _same_stamp(context.stamp, request.expected):
                raise GatewayError("version_conflict", 409)
            source = (await session.execute(text("SELECT * FROM catalog_sources WHERE id=:id FOR UPDATE"),
                                            {"id": request.source_id})).mappings().one()
            operation = (await session.execute(text("SELECT * FROM provider_operations WHERE id=:id FOR UPDATE"),
                                                {"id": request.run_id})).mappings().first()
            if operation is None or operation["source_id"] != request.source_id or operation["state"] != "succeeded":
                raise GatewayError("catalog_snapshot_stale", 409)
            if source["latest_successful_run_id"] != request.run_id:
                raise GatewayError("catalog_snapshot_stale", 409)
            fresh = await session.scalar(text("SELECT clock_timestamp() - finished_at < interval '24 hours' "
                                              "FROM provider_operations WHERE id=:id AND finished_at IS NOT NULL"),
                                         {"id": request.run_id})
            if not fresh:
                raise GatewayError("catalog_snapshot_stale", 409)
            run_expected = _stamp(operation["expected"])
            if not _same_stamp(run_expected, request.expected):
                raise GatewayError("version_conflict", 409)
            entries = {row["upstream_id"]: row for row in
                       (await session.execute(text("SELECT upstream_id,digest FROM catalog_entries WHERE run_id=:run"),
                                               {"run": request.run_id})).mappings().all()}
            if any(item.upstream_id not in entries for item in request.selections):
                raise GatewayError("catalog_conflict", 409)
            if any(not item.capabilities or not item.capabilities <= _CAPABILITIES for item in request.selections):
                raise GatewayError("unsupported_feature")
            await lock_model_names(session, names)

            result_rows = []
            for item in request.selections:
                model_row = (await session.execute(text("SELECT config,version FROM public_models WHERE id=:id FOR UPDATE"),
                                                   {"id": item.public_model_id})).mappings().first()
                if model_row is None:
                    if item.new_model is None or item.expected_model_version is not None:
                        raise GatewayError("catalog_conflict", 409)
                    model = item.new_model
                    if model.identity != item.identity or model.model_id != item.public_model_id:
                        raise GatewayError("catalog_conflict", 409)
                    model_version = await write_model(session, model, create_only=True)
                else:
                    if item.new_model is not None or item.expected_model_version is None:
                        raise GatewayError("catalog_conflict", 409)
                    if model_row["version"] != item.expected_model_version:
                        raise GatewayError("catalog_conflict", 409)
                    model = ModelConfig.model_validate(model_row["config"])
                    if model.identity != item.identity:
                        raise GatewayError("catalog_conflict", 409)
                    model_version = model_row["version"]
                if item.binding_id is None:
                    binding_id, expected_binding, create_only = uuid4(), None, True
                else:
                    binding_id, expected_binding, create_only = item.binding_id, item.binding_version, False
                    if expected_binding is None:
                        raise GatewayError("catalog_conflict", 409)
                binding = BindingConfig(id=binding_id, provider_id=source["provider_id"],
                                       credential_id=source["credential_id"], public_model_id=item.public_model_id,
                                       upstream_model=item.upstream_id, identity=item.identity,
                                       input_bound=item.input_bound, output_bound=item.output_bound,
                                       capabilities=item.capabilities, enabled=item.binding_enabled)
                binding_version = await write_binding(session, binding, expected_version=expected_binding,
                                                      create_only=create_only)
                decision = (await session.execute(text("SELECT * FROM catalog_decisions WHERE source_id=:source AND upstream_id=:upstream FOR UPDATE"),
                                                  {"source": request.source_id, "upstream": item.upstream_id})).mappings().first()
                if decision is not None:
                    if item.decision_version is not None and decision["version"] != item.decision_version:
                        raise GatewayError("catalog_conflict", 409)
                    await session.scalar(text("""UPDATE catalog_decisions SET ignored=false,
                        binding_id=:binding,published_run_id=:run,published_digest=:digest,version=version+1,
                        updated_at=clock_timestamp() WHERE source_id=:source AND upstream_id=:upstream RETURNING version"""),
                        {"binding": binding_id, "run": request.run_id, "digest": entries[item.upstream_id]["digest"],
                         "source": request.source_id, "upstream": item.upstream_id})
                else:
                    if item.decision_version is not None:
                        raise GatewayError("catalog_conflict", 409)
                    await session.scalar(text("""INSERT INTO catalog_decisions
                        (source_id,upstream_id,ignored,binding_id,published_run_id,published_digest)
                        VALUES (:source,:upstream,false,:binding,:run,:digest) RETURNING version"""),
                        {"source": request.source_id, "upstream": item.upstream_id, "binding": binding_id,
                         "run": request.run_id, "digest": entries[item.upstream_id]["digest"]})
                result_rows.append(PublishedBinding(upstream_id=item.upstream_id, public_model_id=item.public_model_id,
                                                   binding_id=binding_id, model_version=model_version,
                                                   binding_version=binding_version))
            result = PublicationResult(operation_id=request.id, run_id=request.run_id, bindings=tuple(result_rows))
            payload = result.model_dump(mode="json")
            await session.execute(text("INSERT INTO catalog_publications(id,source_id,run_id,payload_digest,result) "
                                       "VALUES (:id,:source,:run,:digest,CAST(:result AS jsonb))"),
                                  {"id": request.id, "source": request.source_id, "run": request.run_id,
                                   "digest": digest, "result": json.dumps(payload)})
            await session.execute(text("INSERT INTO audit_events(id,actor,action,record_id,details) "
                                       "VALUES (:id,:actor,'catalog.published',:record,CAST(:details AS jsonb))"),
                                  {"id": uuid4(), "actor": str(actor), "record": request.source_id,
                                   "details": json.dumps({"run_id": str(request.run_id), "count": len(result_rows)})})
            return result
