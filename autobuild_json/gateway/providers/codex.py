from contextlib import asynccontextmanager
from datetime import datetime, timezone
import time

from ..errors import GatewayError, UpstreamRejected
from .retry import parse_retry_after
from ..protocols.openai_responses import ResponsesCodec
from ..transport.http import OutboundRequest
from .openai import ProviderStream
from .responses_events import responses_events, compact_events, ResponsesEvents


def codex_body(request, model):
    body = ResponsesCodec().upstream_body(request, model)
    # The Codex Responses endpoint does not accept these generation controls.
    # They are compatibility hints, not enforced caps: Engine reserves the full
    # certified binding bound even when the client supplies a smaller limit.
    for name in ("max_output_tokens", "temperature", "top_p"):
        body.pop(name, None)
    include = list(body.get("include", []))
    if "reasoning.encrypted_content" not in include:
        include.append("reasoning.encrypted_content")
    body["include"] = include
    body.setdefault("instructions", "")
    if request.image_tool and model.startswith("gpt-image-"):
        body["model"] = "gpt-5.5"
        body["tools"][-1]["model"] = model
    if request.operation == "compact":
        body = {key: value for key, value in body.items() if key in {"model", "input", "instructions"}}
    return body


class CodexAdapter:
    def __init__(self, transport, token_resolver):
        self.transport, self.token_resolver = transport, token_resolver

    @asynccontextmanager
    async def open(self, request, route, lease):
        if route.root != "https://chatgpt.com/backend-api/codex":
            raise GatewayError("egress_denied", 502)
        body = codex_body(request, route.upstream_model)
        account_id, tokens = await self.token_resolver(route)
        remaining = min(route.timeout, (lease.deadline-datetime.now(timezone.utc)).total_seconds())
        if request.operation == "websocket":
            from ..transport.websocket import open_codex_websocket
            async with open_codex_websocket(self.transport, route, lease.proxy, account_id,
                    tokens["access_token"], time.monotonic()+remaining) as connection:
                body.pop("stream", None)
                await connection.send_json(dict(body, type="response.create"))
                yield ProviderStream(connection, websocket_events(connection))
            return
        compact = request.operation == "compact"
        call = OutboundRequest("POST", "responses/compact" if compact else "responses", body, auth_header=("Authorization", "Bearer "+tokens["access_token"]),
            # Keep the name lowercase so Transport's default JSON Accept is
            # replaced instead of becoming a duplicate case-insensitive pair.
            headers=(("chatgpt-account-id", account_id), ("accept", "application/json" if compact else "text/event-stream")), deadline=time.monotonic()+remaining)
        async with self.transport.open(route, lease.proxy, call) as response:
            if response.status in {400, 401, 403, 404, 422, 429}:
                rejected = UpstreamRejected(response.status, parse_retry_after(response.headers.get("retry-after")))
                rejected.received_at = time.monotonic()
                raise rejected
            if response.status != 200:
                raise GatewayError("upstream_error", 502)
            yield ProviderStream(response, compact_events(response) if compact else responses_events(response, images=request.image_tool is not None))


async def websocket_events(connection):
    parser = ResponsesEvents()
    try:
        while not parser.collector.terminal:
            for event in parser.feed(await connection.receive_json()):
                yield event
    except (ValueError, KeyError, TypeError, AttributeError):
        raise GatewayError("upstream_error", 502, "stream") from None
