from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import time

from ..contracts import InferenceEvent, Text, ToolCall
from ..errors import GatewayError, UpstreamRejected
from .retry import parse_retry_after
from ..metering.records import Usage
from ..protocols.frames import SSEDecoder
from ..protocols.openai_chat import OpenAIChatCodec
from ..protocols.openai_responses import ResponsesCodec
from .responses_events import responses_events
from .preflight import auth_header
from ..transport.http import OutboundRequest


def normalize_openai_usage(payload):
    try:
        return Usage(input_tokens=payload["prompt_tokens"], output_tokens=payload["completion_tokens"],
            cached_read=(payload.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            reasoning=(payload.get("completion_tokens_details") or {}).get("reasoning_tokens", 0))
    except (ValueError, KeyError, TypeError, AttributeError):
        raise GatewayError("invalid_usage", 502, "upstream") from None


class ProviderStream:
    def __init__(self, response, events):
        self.response, self.events = response, events

    async def cancel(self):
        await self.response.cancel()


async def chat_events(response):
    decoder = SSEDecoder()
    text_id, tools, finish, usage, index = None, {}, None, None, 0
    done = False
    yield InferenceEvent(kind="started")
    try:
        async for chunk in response.chunks():
            for _, data in decoder.feed(chunk):
                if done:
                    raise ValueError()
                if data == "[DONE]":
                    done = True
                    continue
                value = json.loads(data)
                if "error" in value:
                    raise GatewayError("upstream_error", 502, "stream")
                if value.get("usage") is not None:
                    usage = normalize_openai_usage(value["usage"])
                    yield InferenceEvent(kind="usage", usage=usage)
                choices = value.get("choices", [])
                if not choices:
                    continue
                if len(choices) != 1 or choices[0].get("index", 0) != 0 or finish is not None:
                    raise ValueError()
                choice = choices[0]
                delta = choice.get("delta", {})
                if set(delta)-{"role", "content", "tool_calls"}:
                    raise GatewayError("unsupported_feature", 502, "stream")
                if delta.get("content"):
                    if text_id is None:
                        text_id = "text"
                        yield InferenceEvent(kind="block_started", item_id=text_id, index=index, block=Text(text=""))
                        index += 1
                    yield InferenceEvent(kind="text_delta", item_id=text_id, delta=delta["content"])
                for part in delta.get("tool_calls", []):
                    tool_index = part["index"]
                    function = part.get("function", {})
                    if tool_index not in tools:
                        call_id, name = part["id"], function["name"]
                        if len(tools) >= 128 or call_id in {t[0] for t in tools.values()}:
                            raise ValueError()
                        tools[tool_index] = (call_id, name)
                        yield InferenceEvent(kind="block_started", item_id=call_id, index=index,
                            block=ToolCall(call_id=call_id, name=name))
                        index += 1
                    call_id, name = tools[tool_index]
                    if part.get("id", call_id) != call_id or function.get("name", name) != name:
                        raise ValueError()
                    if function.get("arguments"):
                        yield InferenceEvent(kind="tool_delta", item_id=call_id, delta=function["arguments"])
                if choice.get("finish_reason") is not None:
                    finish = choice["finish_reason"]
                    if finish not in {"stop", "length", "tool_calls", "content_filter"}:
                        raise ValueError()
        decoder.finish()
        if not done or finish is None:
            raise ValueError()
        if text_id is not None:
            yield InferenceEvent(kind="block_finished", item_id=text_id)
        for call_id, _ in tools.values():
            yield InferenceEvent(kind="block_finished", item_id=call_id)
        yield InferenceEvent(kind="finished", finish_reason=finish, usage=usage)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise GatewayError("upstream_error", 502, "stream") from None


class OpenAIAdapter:
    def __init__(self, transport, credential_resolver):
        self.transport, self.credential_resolver = transport, credential_resolver

    @asynccontextmanager
    async def open(self, request, route, lease):
        request.validate_provider("openai_compatible")
        native = getattr(route, "wire_api", "chat") == "responses"
        body = (ResponsesCodec() if native else OpenAIChatCodec()).upstream_body(request, route.upstream_model)
        secret = await self.credential_resolver(route)
        remaining = min(route.timeout, (lease.deadline-datetime.now(timezone.utc)).total_seconds())
        auth_mode = getattr(route, "auth_mode", "bearer")
        auth = auth_header(auth_mode,secret)
        call = OutboundRequest("POST", "responses" if native else "chat/completions", body, auth_header=auth, deadline=time.monotonic()+remaining)
        async with self.transport.open(route, lease.proxy, call) as response:
            if response.status != 200:
                if response.status in {400, 401, 403, 404, 422, 429}:
                    raise UpstreamRejected(response.status, parse_retry_after(response.headers.get("retry-after")))
                raise GatewayError("rate_limited" if response.status == 429 else "upstream_error", 502, "upstream")
            yield ProviderStream(response, responses_events(response) if native else chat_events(response))
