from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import time

from ..errors import GatewayError, UpstreamRejected
from .retry import parse_retry_after
from ..protocols.openai_responses import ResponsesCodec
from ..transport.http import OutboundRequest
from .openai import ProviderStream
from .responses_events import responses_events, compact_events, ResponsesEvents

CODEX_ORIGINATOR = "codex-tui"
# Protocol identification follows the reference client's runtime headers;
# it does not grant account/model access or bypass upstream restrictions.
CODEX_USER_AGENT = "codex-tui/0.153.4 (Linux; x86_64)"


def codex_body(request, model):
    if request.options.reasoning is not None and request.options.reasoning.effort == 'ultra':
        # Real endpoint rejection observed 2026-09-26: max is accepted, ultra
        # is not. Never silently downgrade a customer's requested effort.
        raise GatewayError('unsupported_reasoning_effort')
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
        account_id, tokens = await self.token_resolver(route)
        try:
            async with self._open(request, route, lease, account_id, tokens) as stream:
                yield stream
        except UpstreamRejected as exc:
            if exc.upstream_status == 401:
                # No raw token travels through exception/reporting paths.
                exc.access_token_digest = hashlib.sha256(tokens['access_token'].encode()).digest()
                # A rejected WebSocket upgrade is also pre-generation, just
                # like HTTP authorization rejection; pool pinning still applies.
                exc.safe_retry = True
            raise

    @asynccontextmanager
    async def _open(self, request, route, lease, account_id, tokens):
        if route.root != "https://chatgpt.com/backend-api/codex":
            raise GatewayError("egress_denied", 502)
        body = codex_body(request, route.upstream_model)
        remaining = min(route.timeout, (lease.deadline-datetime.now(timezone.utc)).total_seconds())
        if request.operation == "websocket":
            from ..transport.websocket import open_codex_websocket
            async with open_codex_websocket(self.transport, route, lease.proxy, account_id,
                    tokens["access_token"], time.monotonic()+remaining) as connection:
                body.pop("stream", None)
                await connection.send_json(dict(body, type="response.create"))
                events = websocket_events(connection)
                try:
                    # An upgrade does not yet mean generation was accepted.
                    # Inspect the first lifecycle event before returning control
                    # to Engine; only explicit pre-start rejections may retry.
                    first = await anext(events)
                    async def accepted_events():
                        yield first
                        async for event in events:
                            yield event
                    yield ProviderStream(connection, accepted_events())
                finally:
                    await events.aclose()
            return
        compact = request.operation == "compact"
        call = OutboundRequest("POST", "responses/compact" if compact else "responses", body, auth_header=("Authorization", "Bearer "+tokens["access_token"]),
            # Keep the name lowercase so Transport's default JSON Accept is
            # replaced instead of becoming a duplicate case-insensitive pair.
            headers=(("chatgpt-account-id", account_id), ("originator", CODEX_ORIGINATOR),
                     ("user-agent", CODEX_USER_AGENT),
                     ("accept", "application/json" if compact else "text/event-stream")), deadline=time.monotonic()+remaining)
        async with self.transport.open(route, lease.proxy, call) as response:
            if response.status in {400, 401, 403, 404, 422, 429}:
                received_at = time.monotonic()
                code = None
                if response.status == 400:
                    try:
                        payload = await response.read_json(max_bytes=64 * 1024)
                        error = payload.get('error', {}) if isinstance(payload, dict) else {}
                        if not isinstance(error, dict):
                            error = {}
                        detail = payload.get("detail", error.get('message', '')) if isinstance(payload, dict) else ""
                        if error.get('param') == 'reasoning.effort' and error.get('code') in {'invalid_value', 'unsupported_value'}:
                            code = 'unsupported_reasoning_effort'
                        elif isinstance(detail, str) and "model is not supported" in detail.lower():
                            code = "model_not_supported"
                        elif isinstance(detail, str) and detail:
                            code = "provider_rejected"
                    except GatewayError:
                        code = None
                rejected = UpstreamRejected(response.status, parse_retry_after(response.headers.get("retry-after")), code)
                # Only explicit pre-generation account failures may switch
                # accounts. Generic 400/403, 5xx and stream failures do not.
                rejected.safe_retry = response.status in {401, 429} or code == "model_not_supported"
                rejected.received_at = received_at
                raise rejected
            if response.status != 200:
                raise GatewayError("upstream_error", 502, "upstream", upstream_status=response.status)
            yield ProviderStream(response, compact_events(response) if compact else responses_events(
                response, images=request.image_tool is not None, allow_empty_terminal_output=True))


async def websocket_events(connection):
    parser = ResponsesEvents(allow_empty_terminal_output=True)
    try:
        while not parser.collector.terminal:
            value = await connection.receive_json()
            # Codex sends quota/session advisories before/between response frames.
            # They are neither response lifecycle events nor billed usage;
            # keep the shared Responses parser strict for all other events.
            if value.get('type') in {'codex.rate_limits', 'codex.response.metadata', 'responsesapi.websocket_timing'}:
                continue
            if not parser.collector.started and value.get('type') == 'error':
                status, error = value.get('status'), value.get('error')
                if type(status) is int and status in {400, 401, 403, 404, 422, 429} and isinstance(error, dict):
                    detail = error.get('message', '')
                    code = None
                    if status == 400:
                        if error.get('param') == 'reasoning.effort' and error.get('code') in {'invalid_value', 'unsupported_value'}:
                            code = 'unsupported_reasoning_effort'
                        elif isinstance(detail, str) and 'model is not supported' in detail.lower():
                            code = 'model_not_supported'
                        elif isinstance(detail, str) and detail:
                            code = 'provider_rejected'
                    rejected = UpstreamRejected(status, code=code)
                    rejected.safe_retry = status in {401, 429} or code == 'model_not_supported'
                    rejected.received_at = time.monotonic()
                    raise rejected
            for event in parser.feed(value):
                yield event
    except (ValueError, KeyError, TypeError, AttributeError):
        raise GatewayError("upstream_error", 502, "stream") from None
