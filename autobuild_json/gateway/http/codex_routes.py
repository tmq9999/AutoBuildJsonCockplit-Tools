"""Authenticated Codex compact, Images facade and Responses WebSocket."""
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..errors import GatewayError
from ..protocols.openai_images import ImagesCodec, read_image_request
from ..protocols.openai_responses import ResponsesCodec, output_item, response_usage
from .auth import client_secret, gateway_session
from .streaming import GatewayStreamResponse
from .codex_websocket import mount_websocket


def codex_router(identity, responses, engine, *, allowed_hosts):
    router = APIRouter()
    router.add_api_route('/backend-api/codex/responses', responses, methods=['POST'])

    @router.post('/v1/responses/compact')
    @router.post('/backend-api/codex/responses/compact')
    async def compact(request: Request):
        principal = await identity.authenticate(client_secret(request), "openai")
        try:
            body = await request.json()
        except ValueError:
            raise GatewayError("invalid_request") from None
        if not isinstance(body, dict):
            raise GatewayError("invalid_request")
        if set(body)-{"model", "input", "instructions"}:
            raise GatewayError("unsupported_feature")
        decoded = ResponsesCodec().decode(body, {}).model_copy(update={"operation": "compact"})
        meta = engine.meta({"operation": "compact", "body": body}, request.headers.get("idempotency-key"),
            session_digest=await engine.session_digest(principal, decoded.model, gateway_session(request)))
        prepared = await engine.prepare(principal, decoded, meta)
        result = await prepared.collect()
        return JSONResponse({"id": result.id, "object": "response.compaction", "created_at": int(time.time()),
            "output": [output_item(block, f"item_{index}") for index, block in enumerate(result.blocks)],
            "usage": response_usage(result.usage)}, headers={"Cache-Control": "no-store"})

    @router.post('/v1/images/generations')
    @router.post('/v1/images/edits')
    async def images(request: Request):
        principal = await identity.authenticate(client_secret(request), "openai")
        body = await read_image_request(request)
        codec = ImagesCodec(edit=request.url.path.endswith("/edits"))
        decoded = codec.decode(body)
        prepared = await engine.prepare(principal, decoded,
            engine.meta({"operation": request.url.path, "body": body}, request.headers.get("idempotency-key")))
        if decoded.stream:
            return GatewayStreamResponse(prepared, codec)
        return JSONResponse(codec.encode_result(await prepared.collect()), headers={"Cache-Control": "no-store"})

    mount_websocket(router, identity, engine, allowed_hosts)
    return router
