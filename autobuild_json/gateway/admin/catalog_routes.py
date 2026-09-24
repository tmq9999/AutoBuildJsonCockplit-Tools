"""Authenticated, local-only views and durable catalog commands."""

from uuid import UUID

from fastapi import Query
from sqlalchemy import text

from ..catalog.records import DecisionEdit, OperationRequest, PublicationRequest
from ..errors import GatewayError
from ..providers.presets import list_presets
from .catalog_schemas import (DecisionsInput, EnqueueInput, ProbeEnqueueInput,
                              PublishInput, ReconcileInput, SourceCreateInput, SourceUpdateInput)
from .schemas import VersionInput
from .serialization import ExactJSONResponse


def wire(value):
    # Serialize before FastAPI's generic Decimal encoder can turn money into floats.
    return value.model_dump(mode="json")


def mount_catalog_routes(router, services):
    @router.get("/provider-presets")
    async def provider_presets():
        return {"presets": list_presets()}

    @router.get("/catalog/sources")
    async def source_list(provider_id: UUID | None = None):
        values = await services.sources.list()
        if provider_id is not None:
            values = [value for value in values if value.provider_id == provider_id]
        available = await services.catalog_worker.available()
        result = []
        for value in values:
            expected, error = None, None
            try:
                expected = wire(await services.sources.current_stamp(value.id))
            except GatewayError as exc:
                error = exc.code
            async with services.db.sessions() as session:
                row = (await session.execute(text("SELECT id,state FROM provider_operations WHERE source_id=:id "
                    "ORDER BY created_at DESC,id DESC LIMIT 1"), {"id": value.id})).mappings().first()
            result.append({**wire(value), "expected": expected, "configuration_error": error,
                "recent_job": {"id": str(row["id"]), "state": row["state"]} if row else None,
                "worker_available": available,
                "protected_over_limit": await services.retention.protected_count(value.id)})
        async with services.db.sessions() as session:
            rows = (await session.execute(text("SELECT provider_id,model_id FROM discovered_models "
                "WHERE CAST(:provider AS uuid) IS NULL OR provider_id=CAST(:provider AS uuid) ORDER BY provider_id,model_id"),
                {"provider": provider_id})).mappings().all()
        return {"sources": result, "legacy": [{"provider_id": str(r["provider_id"]),
                "upstream_id": r["model_id"], "provenance": "legacy"} for r in rows]}

    @router.post("/catalog/sources", status_code=201)
    async def source_create(payload: SourceCreateInput):
        return wire(await services.sources.create(payload.domain(), "admin"))

    @router.put("/catalog/sources/{identity}")
    async def source_update(identity: UUID, payload: SourceUpdateInput):
        return wire(await services.sources.update(identity, payload.version, payload.domain(), "admin"))

    async def enqueue(identity, kind, payload):
        services.build_catalog_worker()
        command = OperationRequest(id=payload.operation_id, source_id=identity, kind=kind,
            expected=payload.expected, probe=payload.probe if isinstance(payload, ProbeEnqueueInput) else None)
        # Claim existence is only a response-status hint; store transaction owns
        # replay, digest and source serialization. No job is tied to this request.
        try:
            await services.operations.get(command.id)
            replay = True
        except GatewayError as exc:
            if exc.code != "not_found":
                raise
            replay = False
        if kind == "probe" and not replay:
            if services.operation_runner.probe is None:
                raise GatewayError("unsupported_feature", 422)
            quote = await services.quotes.quote(identity, command.probe.binding_id)
            if (quote.stamp != command.expected or quote.digest != command.probe.quote_digest
                    or quote.upper_cost != command.probe.max_hold or quote.currency != command.probe.currency):
                raise GatewayError("version_conflict", 409)
        view = await services.operations.enqueue(command, "admin")
        return ExactJSONResponse(wire(view), status_code=200 if replay or view.id != command.id else 202)

    @router.post("/catalog/sources/{identity}/discover", status_code=202)
    async def discover(identity: UUID, payload: EnqueueInput):
        return await enqueue(identity, "discover", payload)

    @router.post("/catalog/sources/{identity}/check", status_code=202)
    async def check(identity: UUID, payload: EnqueueInput):
        return await enqueue(identity, "check", payload)

    @router.post("/catalog/sources/{identity}/probe", status_code=202)
    async def probe(identity: UUID, payload: ProbeEnqueueInput):
        return await enqueue(identity, "probe", payload)

    @router.get("/provider-operations/{identity}")
    async def operation_get(identity: UUID):
        return wire(await services.operations.get(identity))

    @router.post("/provider-operations/{identity}/cancel")
    async def operation_cancel(identity: UUID, payload: VersionInput):
        canonical = await services.operations.get(identity)
        return wire(await services.operations.cancel(canonical.id, payload.version, "admin"))

    @router.post("/provider-operations/{identity}/reconcile")
    async def operation_reconcile(identity: UUID, payload: ReconcileInput):
        canonical = await services.operations.get(identity)
        return wire(await services.probe_accounting.reconcile(canonical.id, payload.version, payload.amount(),
                                                              payload.currency, payload.reason, "admin"))

    @router.get("/catalog/sources/{identity}/entries")
    async def entries(identity: UUID, cursor: str | None = Query(default=None, max_length=4096),
                      limit: int = Query(default=50, ge=1, le=200), diff: str | None = None):
        return wire(await services.snapshots.page(identity, cursor=cursor, limit=limit, diff=diff))

    @router.put("/catalog/sources/{identity}/decisions")
    async def decisions(identity: UUID, payload: DecisionsInput):
        edits = tuple(DecisionEdit(**item.model_dump()) for item in payload.decisions)
        return {"decisions": await services.decisions.apply(identity, payload.version, edits, "admin")}

    @router.get("/catalog/sources/{identity}/probe-quote")
    async def probe_quote(identity: UUID, binding_id: UUID):
        return wire(await services.quotes.quote(identity, binding_id))

    @router.post("/catalog/sources/{identity}/publish")
    async def publish(identity: UUID, payload: PublishInput):
        command = PublicationRequest(id=payload.operation_id, source_id=identity, run_id=payload.run_id,
                                     expected=payload.expected, selections=tuple(payload.selections))
        return wire(await services.publisher.publish(command, "admin"))
