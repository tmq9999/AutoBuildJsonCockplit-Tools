from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import time
from uuid import uuid4

from ..contracts import InferenceEvent, Text, ToolCall
from ..errors import GatewayError, UpstreamRejected
from .retry import parse_retry_after
from ..metering.records import Usage
from ..protocols.ollama import OllamaCodec
from ..protocols.frames import NDJSONDecoder
from ..transport.http import OutboundRequest
from .openai import ProviderStream
from .preflight import auth_header


async def ollama_events(response):
    parser = NDJSONDecoder()
    text_started, done, has_tools, index = False, False, False, 0
    yield InferenceEvent(kind="started")
    try:
        async for chunk in response.chunks():
            for payload in parser.feed(chunk):
                if done or "error" in payload:
                    raise ValueError()
                message = payload.get("message", {})
                if set(message)-{"role", "content", "tool_calls"}:
                    raise GatewayError("unsupported_feature", 502)
                if message.get("content"):
                    if not text_started:
                        yield InferenceEvent(kind="block_started", item_id="text", index=index, block=Text(text=""))
                        index += 1
                        text_started = True
                    yield InferenceEvent(kind="text_delta", item_id="text", delta=message["content"])
                for call in message.get("tool_calls", []):
                    identity = "call_"+uuid4().hex
                    has_tools = True
                    yield InferenceEvent(kind="block_started", item_id=identity, index=index, block=ToolCall(call_id=identity,
                        name=call["function"]["name"], arguments=json.dumps(call["function"]["arguments"])))
                    yield InferenceEvent(kind="block_finished", item_id=identity)
                    index += 1
                if payload.get("done") is True:
                    done = True
                    if text_started:
                        yield InferenceEvent(kind="block_finished", item_id="text")
                    usage = Usage(payload["prompt_eval_count"], payload["eval_count"]) if {"prompt_eval_count", "eval_count"} <= payload.keys() else None
                    yield InferenceEvent(kind="finished", finish_reason="length" if payload.get("done_reason") == "length" else "tool_calls" if has_tools else "stop", usage=usage)
        parser.finish()
        if not done:
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise GatewayError("upstream_error", 502, "stream") from None


class OllamaAdapter:
    def __init__(self, transport, credential_resolver):
        self.transport, self.credential_resolver = transport, credential_resolver

    @asynccontextmanager
    async def open(self, request, route, lease):
        body = OllamaCodec().upstream_body(request, route.upstream_model)
        auth = auth_header(route.auth_mode,await self.credential_resolver(route))
        remaining = min(route.timeout, (lease.deadline-datetime.now(timezone.utc)).total_seconds())
        call = OutboundRequest("POST", "chat", body, auth_header=auth, deadline=time.monotonic()+remaining)
        async with self.transport.open(route, lease.proxy, call) as response:
            if response.status in {400, 401, 403, 404, 422, 429}:
                raise UpstreamRejected(response.status, parse_retry_after(response.headers.get("retry-after")))
            if response.status != 200:
                raise GatewayError("upstream_error", 502)
            yield ProviderStream(response, ollama_events(response))
