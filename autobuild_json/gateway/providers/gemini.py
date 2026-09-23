from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import time
from urllib.parse import quote
from uuid import uuid4

from ..contracts import InferenceEvent, Text, ToolCall
from ..errors import GatewayError, UpstreamRejected
from ..metering.records import Usage
from ..protocols.gemini import GeminiCodec
from ..protocols.frames import SSEDecoder
from ..transport.http import OutboundRequest
from .openai import ProviderStream


def normalize_gemini_usage(payload):
    thoughts = payload.get("thoughtsTokenCount", 0)
    return Usage(payload["promptTokenCount"], payload["candidatesTokenCount"]+thoughts,
                 cached_read=payload.get("cachedContentTokenCount", 0), reasoning=thoughts)


async def gemini_events(response):
    decoder = SSEDecoder()
    usage, finish, text_started, index = None, None, False, 0
    has_tools = False
    yield InferenceEvent(kind="started")
    try:
        async for chunk in response.chunks():
            for _, data in decoder.feed(chunk):
                value = json.loads(data)
                if value.get("usageMetadata"):
                    usage = normalize_gemini_usage(value["usageMetadata"])
                candidates = value.get("candidates", [])
                if not candidates:
                    continue
                if len(candidates) != 1 or candidates[0].get("index", 0) != 0:
                    raise ValueError()
                candidate = candidates[0]
                for part in candidate.get("content", {}).get("parts", []):
                    if set(part) == {"text"}:
                        if not text_started:
                            text_started = True
                            yield InferenceEvent(kind="block_started", item_id="text", index=index, block=Text(text=""))
                            index += 1
                        yield InferenceEvent(kind="text_delta", item_id="text", delta=part["text"])
                    elif set(part) == {"functionCall"}:
                        has_tools = True
                        call = part["functionCall"]
                        identity = call.get("id") or "call_"+uuid4().hex
                        yield InferenceEvent(kind="block_started", item_id=identity, index=index,
                                             block=ToolCall(call_id=identity, name=call["name"], arguments=json.dumps(call["args"])))
                        yield InferenceEvent(kind="block_finished", item_id=identity)
                        index += 1
                    else:
                        raise GatewayError("unsupported_feature", 502)
                if "finishReason" in candidate:
                    finish = {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "content_filter"}[candidate["finishReason"]]
        decoder.finish()
        if finish is None:
            raise ValueError()
        if text_started:
            yield InferenceEvent(kind="block_finished", item_id="text")
        yield InferenceEvent(kind="finished", finish_reason="tool_calls" if has_tools and finish == "stop" else finish, usage=usage)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise GatewayError("upstream_error", 502, "stream") from None


class GeminiAdapter:
    def __init__(self, transport, credential_resolver):
        self.transport, self.credential_resolver = transport, credential_resolver

    async def _call(self, request, route, lease, action):
        body = GeminiCodec().upstream_body(request, route.upstream_model)
        if action == "countTokens":
            body.pop("generationConfig", None)
        secret = await self.credential_resolver(route)
        remaining = min(route.timeout, (lease.deadline-datetime.now(timezone.utc)).total_seconds())
        model = route.upstream_model.removeprefix("models/")
        return OutboundRequest("POST", "models/"+quote(model, safe="")+":"+action, body,
            auth_header=("x-goog-api-key", secret), deadline=time.monotonic()+remaining,
            query=(("alt", "sse"),) if action == "streamGenerateContent" else ())

    @asynccontextmanager
    async def open(self, request, route, lease):
        request.validate_provider("gemini")
        async with self.transport.open(route, lease.proxy, await self._call(request, route, lease, "streamGenerateContent")) as response:
            if response.status in {400, 401, 403, 404, 422, 429}:
                raise UpstreamRejected(response.status)
            if response.status != 200:
                raise GatewayError("upstream_error", 502)
            yield ProviderStream(response, gemini_events(response))

    async def count(self, request, route, lease):
        async with self.transport.open(route, lease.proxy, await self._call(request, route, lease, "countTokens")) as response:
            if response.status != 200:
                raise GatewayError("upstream_error", 502)
            payload = await response.read_json()
            if type(payload.get("totalTokens")) is not int or payload["totalTokens"] < 0:
                raise GatewayError("upstream_error", 502)
            return payload["totalTokens"]
