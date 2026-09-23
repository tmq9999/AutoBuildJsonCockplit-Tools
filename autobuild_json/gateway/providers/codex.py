from contextlib import asynccontextmanager
from datetime import datetime, timezone
import time

from ..errors import GatewayError, UpstreamRejected
from .retry import parse_retry_after
from ..protocols.openai_responses import ResponsesCodec
from ..transport.http import OutboundRequest
from .openai import ProviderStream
from .responses_events import responses_events


def codex_body(request, model):
    if request.options.max_output_tokens is not None or request.options.temperature is not None or request.options.top_p is not None:
        raise GatewayError("unsupported_feature")
    body = ResponsesCodec().upstream_body(request, model)
    body.setdefault("instructions", "")
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
        call = OutboundRequest("POST", "responses", body, auth_header=("Authorization", "Bearer "+tokens["access_token"]),
            headers=(("chatgpt-account-id", account_id),), deadline=time.monotonic()+remaining)
        async with self.transport.open(route, lease.proxy, call) as response:
            if response.status in {400, 401, 403, 404, 422, 429}:
                raise UpstreamRejected(response.status, parse_retry_after(response.headers.get("retry-after")))
            if response.status != 200:
                raise GatewayError("upstream_error", 502)
            yield ProviderStream(response, responses_events(response))
