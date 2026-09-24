from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..errors import GatewayError
from ..protocols.ollama import OllamaCodec
from .auth import client_secret, gateway_session
from .streaming import GatewayStreamResponse


def ollama_router(identity, catalog, engine):
    router = APIRouter()
    @router.get("/api/tags")
    async def tags(request: Request):
        principal = await identity.authenticate(client_secret(request), "ollama")
        values = await catalog.list_model_names(principal)
        return {"models": [{"name": m, "model": m} for m in values]}

    @router.get("/api/version")
    async def version(request: Request):
        await identity.authenticate(client_secret(request), "ollama")
        return {"version": "0.1.0-gateway"}

    @router.post("/api/show")
    async def show(request: Request):
        principal = await identity.authenticate(client_secret(request), "ollama")
        body = await request.json()
        model = body.get("model", body.get("name"))
        if model not in await catalog.list_model_names(principal):
            raise GatewayError("not_found", 404)
        return {"model_info": {"general.name": model}, "capabilities": ["completion"]}

    async def inference(request, generate):
        principal = await identity.authenticate(client_secret(request), "ollama")
        try:
            body = await request.json()
        except ValueError:
            raise GatewayError("invalid_request") from None
        codec = OllamaCodec(generate=generate)
        decoded = codec.decode(body, {})
        prepared = await engine.prepare(principal, decoded, engine.meta(body, request.headers.get("idempotency-key"), protocol="ollama",
            session_digest=await engine.session_digest(principal, decoded.model, gateway_session(request))))
        codec.response_id = prepared.collector.id
        codec.model = prepared.collector.model
        if decoded.stream:
            return GatewayStreamResponse(prepared, codec)
        return JSONResponse(codec.encode_result(await prepared.collect()), headers={"Cache-Control": "no-store"})

    @router.post("/api/chat")
    async def chat(request: Request):
        return await inference(request, False)

    @router.post("/api/generate")
    async def generate(request: Request):
        return await inference(request, True)
    return router
