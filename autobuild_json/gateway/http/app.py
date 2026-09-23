from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from ..errors import GatewayError
from ..protocols.openai_chat import OpenAIChatCodec
from ..protocols.openai_responses import ResponsesCodec
from ..protocols.anthropic import AnthropicCodec
from .auth import client_secret
from .boundary import GatewayBoundary
from .streaming import GatewayStreamResponse


def create_gateway_app(identity, catalog, engine, *, allowed_hosts):
    app = FastAPI(title="AutoBuild API Gateway", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GatewayBoundary, allowed_hosts=allowed_hosts)

    @app.exception_handler(GatewayError)
    async def gateway_error(request, error):
        headers = {"Cache-Control": "no-store"}
        if error.retry_after is not None:
            headers["Retry-After"] = str(error.retry_after)
        if request.url.path.startswith("/v1/messages") or request.headers.get("anthropic-version"):
            kind = {401: "authentication_error", 403: "permission_error", 429: "rate_limit_error"}.get(error.status, "invalid_request_error" if error.status == 400 else "api_error")
            return JSONResponse({"type": "error", "error": {"type": kind, "message": error.code}}, status_code=error.status, headers=headers)
        return JSONResponse(error.to_dict(), status_code=error.status, headers=headers)

    @app.exception_handler(SQLAlchemyError)
    async def unavailable(request, error):
        return JSONResponse(GatewayError("storage_unavailable").to_dict(), status_code=503)

    @app.get("/v1/models")
    async def models(request: Request):
        anthropic = request.headers.get("anthropic-version") is not None
        if anthropic and request.headers["anthropic-version"] != "2023-06-01":
            raise GatewayError("unsupported_feature")
        principal = await identity.authenticate(client_secret(request), "anthropic" if anthropic else "openai")
        values = await catalog.list_models(principal)
        if anthropic:
            rows = [{"id": model.model_id, "type": "model", "display_name": model.model_id, "created_at": "2026-01-01T00:00:00Z"} for model in values]
            return {"data": rows, "has_more": False, "first_id": rows[0]["id"] if rows else None, "last_id": rows[-1]["id"] if rows else None}
        return {"object": "list", "data": [{"id": model.model_id, "object": "model", "created": 0, "owned_by": "gateway"}
                                           for model in values]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        principal = await identity.authenticate(client_secret(request), "openai")
        try:
            body = await request.json()
        except ValueError:
            raise GatewayError("invalid_request") from None
        codec = OpenAIChatCodec()
        decoded = codec.decode(body, {})
        meta = engine.meta(body, request.headers.get("idempotency-key"))
        prepared = await engine.prepare(principal, decoded, meta)
        codec.response_id = prepared.collector.id
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
        prepared = await engine.prepare(principal, decoded, engine.meta(body, request.headers.get("idempotency-key")))
        codec.response_id = prepared.collector.id
        if decoded.stream:
            return GatewayStreamResponse(prepared, codec)
        return JSONResponse(codec.encode_result(await prepared.collect()), headers={"Cache-Control": "no-store"})

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
        prepared = await engine.prepare(principal, decoded, engine.meta(body, request.headers.get("idempotency-key"), protocol="anthropic"))
        codec.response_id = prepared.collector.id
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

    return app
