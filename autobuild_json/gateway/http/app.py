from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from ..errors import GatewayError
from ..protocols.openai_chat import OpenAIChatCodec
from ..protocols.openai_responses import ResponsesCodec
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
        return JSONResponse(error.to_dict(), status_code=error.status, headers=headers)

    @app.exception_handler(SQLAlchemyError)
    async def unavailable(request, error):
        return JSONResponse(GatewayError("storage_unavailable").to_dict(), status_code=503)

    @app.get("/v1/models")
    async def models(request: Request):
        principal = await identity.authenticate(client_secret(request), "openai")
        values = await catalog.list_models(principal)
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

    return app
