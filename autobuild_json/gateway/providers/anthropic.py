from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import time

from ..contracts import InferenceEvent, Text, ToolCall
from ..errors import GatewayError, UpstreamRejected
from .retry import parse_retry_after
from ..metering.records import Usage
from ..protocols.anthropic import AnthropicCodec
from ..protocols.frames import SSEDecoder
from ..transport.http import OutboundRequest
from .openai import ProviderStream
from .preflight import auth_header


def normalize_anthropic_usage(payload):
    read = payload.get("cache_read_input_tokens", 0)
    write = payload.get("cache_creation_input_tokens", 0)
    return Usage(payload["input_tokens"]+read+write, payload["output_tokens"], cached_read=read, cached_write=write)


async def anthropic_events(response):
    decoder = SSEDecoder()
    totals, stop = {}, None
    started = done = False
    try:
        async for chunk in response.chunks():
            for _, data in decoder.feed(chunk):
                item = json.loads(data)
                kind = item["type"]
                if done:
                    raise ValueError()
                if kind == "ping":
                    continue
                if kind == "message_start":
                    if started:
                        raise ValueError()
                    started = True
                    totals.update(item["message"].get("usage", {}))
                    yield InferenceEvent(kind="started")
                elif kind == "content_block_start":
                    block = item["content_block"]
                    if block["type"] == "text":
                        parsed = Text(text=block["text"])
                    elif block["type"] == "tool_use":
                        parsed = ToolCall(call_id=block["id"], name=block["name"], arguments="")
                    else:
                        raise GatewayError("unsupported_feature", 502)
                    yield InferenceEvent(kind="block_started", item_id=str(item["index"]), index=item["index"], block=parsed)
                elif kind == "content_block_delta":
                    delta = item["delta"]
                    if delta["type"] not in {"text_delta", "input_json_delta"}:
                        raise GatewayError("unsupported_feature", 502)
                    yield InferenceEvent(kind="text_delta" if delta["type"] == "text_delta" else "tool_delta",
                        item_id=str(item["index"]), delta=delta.get("text", delta.get("partial_json", "")))
                elif kind == "content_block_stop":
                    yield InferenceEvent(kind="block_finished", item_id=str(item["index"]))
                elif kind == "message_delta":
                    totals.update(item.get("usage", {}))
                    stop = item["delta"].get("stop_reason", stop)
                elif kind == "message_stop":
                    done = True
                    reason = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls",
                              "refusal": "content_filter"}[stop]
                    usage = normalize_anthropic_usage(totals) if {"input_tokens", "output_tokens"} <= totals.keys() else None
                    yield InferenceEvent(kind="finished", finish_reason=reason, usage=usage)
                else:
                    raise GatewayError("upstream_error", 502, "stream")
        decoder.finish()
        if not started or not done:
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise GatewayError("upstream_error", 502, "stream") from None


class AnthropicAdapter:
    def __init__(self, transport, credential_resolver):
        self.transport, self.credential_resolver = transport, credential_resolver

    async def count(self, request, route, lease):
        body = AnthropicCodec().upstream_body(request, route.upstream_model)
        for key in ("stream", "max_tokens", "temperature", "top_p", "stop_sequences"):
            body.pop(key, None)
        secret = await self.credential_resolver(route)
        remaining = min(route.timeout, (lease.deadline-datetime.now(timezone.utc)).total_seconds())
        call = OutboundRequest("POST", "messages/count_tokens", body, auth_header=auth_header(route.auth_mode,secret),
                              headers=(("anthropic-version", "2023-06-01"),), deadline=time.monotonic()+remaining)
        async with self.transport.open(route, lease.proxy, call) as response:
            if response.status in {400, 401, 403, 404, 422, 429}:
                raise UpstreamRejected(response.status, parse_retry_after(response.headers.get("retry-after")))
            if response.status != 200:
                raise GatewayError("upstream_error", 502)
            payload = await response.read_json()
            if not isinstance(payload, dict) or type(payload.get("input_tokens")) is not int or payload["input_tokens"] < 0:
                raise GatewayError("upstream_error", 502)
            return payload["input_tokens"]

    @asynccontextmanager
    async def open(self, request, route, lease, *, on_rejected=None):
        request.validate_provider("anthropic")
        body = AnthropicCodec().upstream_body(request, route.upstream_model)
        secret = await self.credential_resolver(route)
        remaining = min(route.timeout, (lease.deadline-datetime.now(timezone.utc)).total_seconds())
        call = OutboundRequest("POST", "messages", body, auth_header=auth_header(route.auth_mode,secret),
            headers=(("anthropic-version", "2023-06-01"),), deadline=time.monotonic()+remaining)
        async with self.transport.open(route, lease.proxy, call) as response:
            if response.status in {400, 401, 403, 404, 422, 429}:
                rejected = UpstreamRejected(response.status, parse_retry_after(response.headers.get("retry-after")))
                if on_rejected is not None:
                    await on_rejected(rejected)
                raise rejected
            if response.status != 200:
                raise GatewayError("upstream_error", 502)
            yield ProviderStream(response, anthropic_events(response))
