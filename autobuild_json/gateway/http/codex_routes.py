"""Exact tested HTTP alias and explicit unsupported Codex transports."""
from fastapi import APIRouter, Request, WebSocket

from ..errors import GatewayError
from .auth import client_secret


def codex_router(identity, responses):
    router = APIRouter()
    router.add_api_route('/backend-api/codex/responses', responses, methods=['POST'])

    @router.post('/v1/responses/compact')
    @router.post('/backend-api/codex/responses/compact')
    @router.post('/v1/images/generations')
    @router.post('/v1/images/edits')
    async def unsupported(request: Request):
        await identity.authenticate(client_secret(request), 'openai')
        raise GatewayError('unsupported_feature')

    @router.websocket('/v1/responses')
    @router.websocket('/backend-api/codex/responses')
    async def unsupported_websocket(websocket: WebSocket):
        await websocket.close(code=1008)

    return router
