from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..errors import GatewayError
from ..protocols.gemini import GeminiCodec, validate_query
from .auth import client_secret, gateway_session
from .streaming import GatewayStreamResponse


def gemini_router(identity, catalog, engine):
    router = APIRouter()
    @router.get("/v1beta/models")
    async def models(request: Request):
        principal = await identity.authenticate(client_secret(request), "gemini")
        values = await catalog.list_models(principal)
        return {"models": [{"name": "models/"+m.model_id, "displayName": m.model_id,
                            "supportedGenerationMethods": ["generateContent", "streamGenerateContent"]} for m in values]}

    @router.get("/v1beta/models/{model:path}")
    async def model_detail(model: str, request: Request):
        principal = await identity.authenticate(client_secret(request), "gemini")
        values = await catalog.list_models(principal)
        if model not in {m.model_id for m in values}:
            raise GatewayError("not_found", 404)
        return {"name": "models/"+model, "displayName": model,
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"]}

    @router.post("/v1beta/models/{target:path}")
    async def generate(target: str, request: Request):
        principal = await identity.authenticate(client_secret(request), "gemini")
        model, _, action = target.rpartition(":")
        if action not in {"generateContent", "streamGenerateContent", "countTokens"} or not model:
            raise GatewayError("unsupported_feature")
        streaming = action == "streamGenerateContent"
        try:
            validate_query(dict(request.query_params), streaming)
            body = await request.json()
        except ValueError:
            raise GatewayError("invalid_request") from None
        codec = GeminiCodec()
        decoded = codec.decode(body, {"model": model, "stream": streaming})
        meta = engine.meta(body, request.headers.get("idempotency-key"), protocol="gemini",
                           session_digest=await engine.session_digest(principal, decoded.model, gateway_session(request)))
        if action == "countTokens":
            return {"totalTokens": await engine.count_tokens(principal, decoded, meta)}
        prepared = await engine.prepare(principal, decoded, meta)
        codec.response_id = prepared.collector.id
        if streaming:
            return GatewayStreamResponse(prepared, codec)
        return JSONResponse(codec.encode_result(await prepared.collect()), headers={"Cache-Control": "no-store"})
    return router
