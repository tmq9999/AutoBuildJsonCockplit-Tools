from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import text

from ..errors import GatewayError
from ..protocols.openai_chat import OpenAIChatCodec
from ..protocols.openai_responses import ResponsesCodec
from ..protocols.anthropic import AnthropicCodec
from .auth import client_secret, gateway_session
from .boundary import GatewayBoundary
from .streaming import GatewayStreamResponse
from .gemini_routes import gemini_router
from .ollama_routes import ollama_router
from .codex_routes import codex_router


def create_gateway_app(identity, catalog, engine, *, allowed_hosts, background_services=None):
    @asynccontextmanager
    async def lifespan(app):
        try:
            await engine.start_capacity_publisher()
            if background_services is not None:
                background_services.quota_refresh_jobs.start()
            yield
        finally:
            # Always join the owned publisher, including a failed background
            # service startup; service cleanup must still run if a flush fails.
            try:
                await engine.stop_capacity_publisher()
            finally:
                try:
                    await engine.drain_prepared_cleanup()
                finally:
                    if background_services is not None:
                        await background_services.close()

    app = FastAPI(title="AutoBuild API Gateway", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GatewayBoundary, allowed_hosts=allowed_hosts)

    @app.exception_handler(GatewayError)
    async def gateway_error(request, error):
        headers = {"Cache-Control": "no-store"}
        if error.retry_after is not None:
            headers["Retry-After"] = str(error.retry_after)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": error.code}, status_code=error.status, headers=headers)
        if request.url.path.startswith("/v1beta/"):
            status = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND",
                      429: "RESOURCE_EXHAUSTED", 503: "UNAVAILABLE"}.get(error.status, "INTERNAL")
            return JSONResponse({"error": {"code": error.status, "message": error.code, "status": status}}, status_code=error.status, headers=headers)
        if request.url.path.startswith("/v1/messages") or request.headers.get("anthropic-version"):
            kind = {401: "authentication_error", 403: "permission_error", 429: "rate_limit_error"}.get(error.status, "invalid_request_error" if error.status == 400 else "api_error")
            return JSONResponse({"type": "error", "error": {"type": kind, "message": error.code}}, status_code=error.status, headers=headers)
        return JSONResponse(error.to_dict(), status_code=error.status, headers=headers)

    @app.exception_handler(SQLAlchemyError)
    async def unavailable(request, error):
        # Keep database failures on the same protocol-shaped, redacted error
        # boundary as explicit GatewayError paths. Returning the generic
        # OpenAI envelope here made Anthropic/Gemini/Ollama clients receive an
        # invalid wire shape and dropped the storage stage.
        return await gateway_error(request, GatewayError("storage_unavailable", 503, "storage"))

    @app.get("/models")
    @app.get("/backend-api/codex/models")
    @app.get("/v1/models")
    async def models(request: Request):
        anthropic = request.headers.get("anthropic-version") is not None
        if anthropic and request.headers["anthropic-version"] != "2023-06-01":
            raise GatewayError("unsupported_feature")
        principal = await identity.authenticate(client_secret(request), "anthropic" if anthropic else "openai")
        values = await catalog.list_model_names(principal)
        if not anthropic and "client_version" in request.query_params:
            from ..providers.codex_model_catalog import client_model
            return {"models": [client_model(value) for value in values]}
        if anthropic:
            rows = [{"id": model, "type": "model", "display_name": model, "created_at": "2026-01-01T00:00:00Z"} for model in values]
            return {"data": rows, "has_more": False, "first_id": rows[0]["id"] if rows else None, "last_id": rows[-1]["id"] if rows else None}
        return {"object": "list", "data": [{"id": model, "object": "model", "created": 0, "owned_by": "gateway"}
                                           for model in values]}

    @app.get("/health")
    async def health():
        try:
            async with engine.db.sessions() as session:
                revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
            if revision is None:
                raise GatewayError("storage_unavailable", 503)
            return {"status": "ok"}
        except SQLAlchemyError:
            return JSONResponse({"status": "unavailable"}, status_code=503)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        principal = await identity.authenticate(client_secret(request), "openai")
        try:
            body = await request.json()
        except ValueError:
            raise GatewayError("invalid_request") from None
        codec = OpenAIChatCodec()
        decoded = codec.decode(body, {})
        meta = engine.meta(body, request.headers.get("idempotency-key"),
                           session_digest=await engine.session_digest(principal, decoded.model, gateway_session(request)))
        prepared = await engine.prepare(principal, decoded, meta)
        codec.response_id = prepared.collector.id
        codec.model = prepared.collector.model
        if decoded.stream:
            return GatewayStreamResponse(prepared, codec)
        return JSONResponse(codec.encode_result(await prepared.collect()), headers={"Cache-Control": "no-store"})

    @app.post("/v1/responses")
    async def responses(request: Request):
        principal = await identity.authenticate(client_secret(request), "openai")
        try:
            body = await request.json()
        except ValueError:
            raise GatewayError("invalid_request") from None
        codec = ResponsesCodec()
        decoded = codec.decode(body, {})
        prepared = await engine.prepare(principal, decoded, engine.meta(body, request.headers.get("idempotency-key"),
            session_digest=await engine.session_digest(principal, decoded.model, gateway_session(request))))
        codec.response_id = prepared.collector.id
        codec.model = prepared.collector.model
        if decoded.stream:
            return GatewayStreamResponse(prepared, codec)
        return JSONResponse(codec.encode_result(await prepared.collect()), headers={"Cache-Control": "no-store"})

    app.include_router(codex_router(identity, responses, engine, allowed_hosts=allowed_hosts))

    @app.post("/v1/messages")
    async def messages(request: Request):
        principal = await identity.authenticate(client_secret(request), "anthropic")
        if request.headers.get("anthropic-version") != "2023-06-01" or request.headers.get("anthropic-beta"):
            raise GatewayError("unsupported_feature")
        try:
            body = await request.json()
        except ValueError:
            raise GatewayError("invalid_request") from None
        codec = AnthropicCodec()
        decoded = codec.decode(body, {})
        prepared = await engine.prepare(principal, decoded, engine.meta(body, request.headers.get("idempotency-key"), protocol="anthropic",
            session_digest=await engine.session_digest(principal, decoded.model, gateway_session(request))))
        codec.response_id = prepared.collector.id
        codec.model = prepared.collector.model
        if decoded.stream:
            return GatewayStreamResponse(prepared, codec)
        return JSONResponse(codec.encode_result(await prepared.collect()), headers={"Cache-Control": "no-store"})

    @app.post("/v1/messages/count_tokens")
    async def message_count(request: Request):
        principal = await identity.authenticate(client_secret(request), "anthropic")
        if request.headers.get("anthropic-version") != "2023-06-01" or request.headers.get("anthropic-beta"):
            raise GatewayError("unsupported_feature")
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError()
        except ValueError:
            raise GatewayError("invalid_request") from None
        decoded = AnthropicCodec().decode(dict(body, max_tokens=1), {})
        return {"input_tokens": await engine.count_tokens(principal, decoded, engine.meta(body, protocol="anthropic"))}

    app.include_router(gemini_router(identity, catalog, engine))
    app.include_router(ollama_router(identity, catalog, engine))
    return app
