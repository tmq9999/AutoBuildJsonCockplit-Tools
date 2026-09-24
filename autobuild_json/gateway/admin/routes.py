from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..errors import GatewayError
from ..routing.records import ProviderConfig, ModelConfig, BindingConfig
from .schemas import CustomerInput, KeyInput, KeyUpdate, VersionInput, EnabledInput, CredentialInput, ProxyInput, AdjustmentInput, OAuthImportInput, PlaygroundInput
from .usage import UsageReports
from .serialization import ExactJSONResponse, exact_input
from .schemas import CredentialRotateInput


class SafeAdminRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request):
            try:
                if request.method in {'POST','PUT','PATCH','DELETE'} and await request.body():
                    request._json=exact_input(await request.json())
                response = await original(request)
            except RequestValidationError:
                response = JSONResponse({"error": "INVALID_REQUEST", "reason": "Invalid request fields"}, status_code=422)
            except GatewayError as exc:
                headers = {"Retry-After": str(exc.retry_after)} if type(exc.retry_after) is int and 0 <= exc.retry_after <= 86400 else None
                catalog = (request.url.path.startswith(("/api/service/catalog/", "/api/service/provider-operations/"))
                           or request.url.path.endswith("/discover"))
                codex = request.url.path.startswith(("/api/service/oauth-accounts", "/api/service/account-pools/",
                    "/api/service/reset-requests/", "/api/service/usage/accounts", "/api/service/usage/requests")) or request.url.path.endswith("/quota-adjust")
                status = 422 if (catalog or codex) and exc.status == 400 else exc.status
                response = JSONResponse(exc.to_dict(), status_code=status, headers=headers)
            except SQLAlchemyError:
                response = JSONResponse({"error": "STORAGE_UNAVAILABLE"}, status_code=503)
            except ValueError:
                response = JSONResponse({'error':'INVALID_REQUEST'},status_code=422)
            response.headers["Cache-Control"] = "no-store"
            return response
        return handle


def create_admin_router(services, authorized):
    router = APIRouter(prefix="/api/service", dependencies=[Depends(authorized)], route_class=SafeAdminRoute,
                       default_response_class=ExactJSONResponse)
    reports = UsageReports(services.db)

    @router.get("/customers")
    async def customers():
        async with services.db.sessions() as session:
            return [dict(row) for row in (await session.execute(text("SELECT id,name,enabled,version FROM customers ORDER BY name"))).mappings()]

    @router.post("/customers", status_code=201)
    async def create_customer(payload: CustomerInput):
        return {"id": await services.identity.create_customer(payload.name)}

    @router.patch("/customers/{identity}")
    async def update_customer(identity: UUID, payload: EnabledInput):
        await services.identity.set_customer_enabled(identity, payload.enabled, version=payload.version, name=payload.name)
        return {"id": identity}

    @router.get("/keys")
    async def keys():
        return await services.identity.admin_key_balances()

    @router.post("/keys", status_code=201)
    async def create_key(payload: KeyInput):
        return await services.identity.create_key(payload.customer_id, payload.policy, name=payload.name)

    @router.patch("/keys/{identity}")
    async def update_key(identity: UUID, payload: KeyUpdate):
        return await services.identity.update_policy(identity, payload.version, payload.policy, name=payload.name)

    @router.post("/keys/{identity}/rotate")
    async def rotate_key(identity: UUID, payload: VersionInput):
        return await services.identity.rotate(identity, payload.version)

    @router.post("/keys/{identity}/revoke")
    async def revoke_key(identity: UUID, payload: VersionInput):
        await services.identity.revoke(identity, payload.version)
        return {"id": identity, "revoked": True}

    @router.get("/providers")
    async def providers():
        async with services.db.sessions() as session:
            return [dict(row) for row in (await session.execute(text("SELECT id,config,version,cooldown_until FROM providers ORDER BY id"))).mappings()]

    @router.post("/providers", status_code=201)
    async def create_provider(payload: ProviderConfig):
        return {"id": await services.catalog.create_provider(payload)}

    @router.put("/providers/{identity}/{version}")
    async def update_provider(identity: UUID, version: int, payload: ProviderConfig):
        await services.catalog.update_provider(identity, version, payload)
        return {"id": identity, "version": version+1}

    @router.delete("/providers/{identity}")
    async def delete_provider(identity: UUID, payload: VersionInput):
        async with services.db.sessions() as session:
            config = await session.scalar(text("SELECT config FROM providers WHERE id=:id"), {"id": identity})
        if config is None:
            raise GatewayError("not_found", 404)
        await services.catalog.update_provider(identity, payload.version,
            ProviderConfig.model_validate(dict(config, enabled=False)))
        return {"id": identity, "disabled": True}

    @router.get("/credentials")
    async def credentials():
        async with services.db.sessions() as session:
            return [dict(row) for row in (await session.execute(text("SELECT id,provider_id,enabled,health,cooldown_until,"
                "account_id,email,token_generation,token_expires_at,profile_id,version FROM credentials ORDER BY id"))).mappings()]

    @router.patch("/credentials/{identity}")
    async def update_credential(identity: UUID, payload: EnabledInput):
        async with services.db.sessions.begin() as session:
            result = await session.scalar(text("UPDATE credentials SET enabled=:enabled,version=version+1 "
                "WHERE id=:id AND version=:version RETURNING id"),
                {"enabled": payload.enabled, "id": identity, "version": payload.version})
            if not result:
                raise GatewayError("version_conflict", 409)
            await services.catalog._audit(session, "credential.enabled" if payload.enabled else "credential.disabled", identity)
        return {"id": identity, "version": payload.version+1}

    @router.post("/providers/{identity}/credentials", status_code=201)
    async def create_credential(identity: UUID, payload: CredentialInput):
        return {"id": await services.catalog.put_credential(identity, payload.secret.get_secret_value())}

    @router.post("/credentials/{identity}/rotate")
    async def rotate_credential(identity: UUID, payload: CredentialRotateInput):
        await services.catalog.rotate_credential(identity, payload.secret.get_secret_value(),expected_version=payload.version)
        return {"id": identity}

    @router.get("/models")
    async def models():
        async with services.db.sessions() as session:
            return [dict(row["config"], version=row["version"]) for row in
                    (await session.execute(text("SELECT config,version FROM public_models ORDER BY id"))).mappings()]

    @router.post("/models", status_code=201)
    async def put_model(payload: ModelConfig):
        async with services.db.sessions() as session:
            if await session.scalar(text('SELECT id FROM public_models WHERE id=:id'),{'id':payload.model_id}):
                raise GatewayError('version_conflict',409)
        async with services.db.sessions.begin() as session:
            from ..providers.registry_writes import write_model
            await write_model(session, payload, create_only=True)
        return {"id": payload.model_id}

    @router.put("/models/{identity:path}/{version}")
    async def update_model(identity: str, version: int, payload: ModelConfig):
        if payload.model_id != identity:
            raise GatewayError("invalid_request")
        async with services.db.sessions.begin() as session:
            from ..providers.registry_writes import write_model
            new_version = await write_model(session, payload, expected_version=version)
            await session.execute(text("INSERT INTO audit_events(id,actor,action,record_id,details) "
                "VALUES (:id,'admin','model.updated',:record,CAST(:details AS jsonb))"),
                {"id": uuid4(), "record": uuid4(), "details": json.dumps({"model_id": identity, "version": new_version})})
        return {"id": identity, "version": new_version}

    @router.get("/bindings")
    async def bindings():
        async with services.db.sessions() as session:
            return [dict(row['config'],version=row['version']) for row in
                    (await session.execute(text("SELECT config,version FROM model_bindings ORDER BY id"))).mappings()]

    @router.post("/bindings", status_code=201)
    async def put_binding(payload: BindingConfig):
        await services.catalog.put_binding(payload,create_only=True)
        return {"id": payload.id}

    @router.put('/bindings/{identity}/{version}')
    async def update_binding(identity:UUID,version:int,payload:BindingConfig):
        if identity!=payload.id:
            raise GatewayError('invalid_request')
        await services.catalog.put_binding(payload,expected_version=version)
        return {'id':identity,'version':version+1}

    @router.delete('/models/{identity:path}')
    async def delete_model(identity:str,payload:VersionInput):
        async with services.db.sessions.begin() as session:
            from ..providers.registry_writes import lock_model_names
            await lock_model_names(session, (identity,))
            row=(await session.execute(text('SELECT config,version FROM public_models WHERE id=:id FOR UPDATE'),{'id':identity})).mappings().first()
            if row is None or row['version']!=payload.version:
                raise GatewayError('version_conflict',409)
            config=dict(row['config'],enabled=False)
            await session.execute(text('UPDATE public_models SET config=CAST(:config AS jsonb),version=version+1 WHERE id=:id'),{'id':identity,'config':json.dumps(config)})
            await services.catalog._audit(session,'model.disabled',uuid4())
        return {'id':identity,'disabled':True}

    @router.get("/proxies")
    async def proxies():
        return await services.profiles.list_profiles()

    @router.post("/proxies", status_code=201)
    async def create_proxy(payload: ProxyInput):
        return {"id": await services.profiles.create(payload)}

    @router.put("/proxies/{identity}/{version}")
    async def update_proxy(identity: UUID, version: int, payload: ProxyInput):
        await services.profiles.update(identity, version, payload)
        return {"id": identity, "version": version+1}

    @router.post("/oauth/import", status_code=201)
    async def import_oauth(payload: OAuthImportInput):
        return {"id": await services.engine.credentials.import_record(payload.record, payload.proxy_profile_id)}

    @router.post("/oauth/{identity}/refresh")
    async def refresh_oauth(identity: UUID):
        await services.engine.credentials.fresh_tokens(identity, datetime.now(timezone.utc)+timedelta(seconds=180))
        return {"id": identity, "status": "refreshed"}

    @router.get("/overview")
    async def overview():
        return await reports.overview()

    @router.get("/usage")
    async def usage():
        return await reports.requests()

    @router.get("/audit")
    async def audit():
        return await reports.audit()

    @router.post("/usage/{identity}/adjust")
    async def adjust(identity: UUID, payload: AdjustmentInput, request: Request):
        await services.ledger.adjust(identity, payload.amount_micro, "admin", payload.reason)
        return {"id": identity}

    from .catalog_schemas import LegacyDiscoverInput
    @router.post('/providers/{identity}/discover')
    async def provider_discovery(identity: UUID, payload: LegacyDiscoverInput, request: Request):
        from .actions import discover
        return await discover(services, identity, request=request)

    @router.post('/playground')
    async def run_playground(payload: PlaygroundInput):
        from .actions import playground
        return await playground(services,payload)

    from .extra import mount_extra
    mount_extra(router,services)
    from .catalog_routes import mount_catalog_routes
    mount_catalog_routes(router, services)
    from .codex_accounts import mount_codex_routes
    mount_codex_routes(router, services)
    return router
