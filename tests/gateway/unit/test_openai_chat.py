import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest


def test_usage_subtotals_are_not_double_counted():
    from autobuild_json.gateway.providers.openai import normalize_openai_usage
    usage = normalize_openai_usage({"prompt_tokens": 100, "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 80}, "completion_tokens_details": {"reasoning_tokens": 20}})
    assert (usage.input_tokens, usage.output_tokens, usage.cached_read, usage.reasoning) == (100, 50, 80, 20)


def test_chat_tool_history_round_trips_without_losing_ids():
    from autobuild_json.gateway.protocols.openai_chat import OpenAIChatCodec
    codec = OpenAIChatCodec()
    body = {"model": "m", "messages": [
        {"role": "system", "content": "rules"}, {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "weather", "arguments": '{"city":"Hanoi"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"}],
        "tools": [{"type": "function", "function": {"name": "weather", "description": "Weather", "parameters": {"type": "object"}}}],
        "max_tokens": 128}
    request = codec.decode(body, {})
    wire = codec.upstream_body(request, "actual-model")
    assert wire["messages"] == body["messages"]
    assert wire["model"] == "actual-model"
    assert wire["tools"] == body["tools"]


@pytest.mark.parametrize("extra", [{"n": 2}, {"response_format": {"type": "json_object"}}, {"unknown": True}])
def test_unsupported_chat_options_are_not_dropped(extra):
    from autobuild_json.gateway.protocols.openai_chat import OpenAIChatCodec
    from autobuild_json.gateway.errors import GatewayError
    with pytest.raises(GatewayError, match="unsupported_feature"):
        OpenAIChatCodec().decode(dict({"model": "m", "messages": [{"role": "user", "content": "hi"}]}, **extra), {})


@pytest.mark.asyncio
async def test_adapter_handles_usage_only_chunk_and_tool_deltas():
    from autobuild_json.gateway.protocols.openai_chat import OpenAIChatCodec
    from autobuild_json.gateway.providers.openai import OpenAIAdapter
    from autobuild_json.gateway.protocols.common import EventCollector
    from autobuild_json.gateway.transport.http import Transport
    from autobuild_json.gateway.transport.egress import EgressPolicy
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_x",
          "type": "function", "function": {"name": "test", "arguments": '{"x":'}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 5}},
    ]
    wire = b"".join(("data: "+json.dumps(c)+"\n\n").encode() for c in chunks)+b"data: [DONE]\n\n"
    observed = []
    def respond(request):
        observed.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer synthetic-provider"
        return httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"})
    async def resolver(host, port):
        return ["93.184.216.34"]
    async def secret(route):
        return "synthetic-provider"
    adapter = OpenAIAdapter(Transport(EgressPolicy(resolver=resolver), adapter=httpx.MockTransport(respond)), secret)
    request = OpenAIChatCodec().decode({"model": "public", "messages": [{"role": "user", "content": "hi"}]}, {})
    route = SimpleNamespace(root="https://provider.invalid/v1", upstream_model="actual", timeout=60, adapter="openai_compatible")
    lease = SimpleNamespace(proxy=None, deadline=datetime.now(timezone.utc)+timedelta(seconds=60))
    collector = EventCollector("r", "public")
    async with adapter.open(request, route, lease) as stream:
        for_event = []
        async for event in stream.events:
            collector.feed(event)
            for_event.append(event.kind)
    result = collector.result()
    assert result.blocks[0].arguments == '{"x":1}' and result.blocks[0].call_id == "call_x"
    assert result.usage.input_tokens == 4 and result.usage.output_tokens == 5
    assert for_event[-1] == "finished"
    assert observed[0]["model"] == "actual" and observed[0]["stream"] is True


def test_chat_stream_encoding_has_role_usage_and_done():
    from autobuild_json.gateway.protocols.openai_chat import OpenAIChatCodec
    from autobuild_json.gateway.contracts import InferenceEvent
    from autobuild_json.gateway.metering.records import Usage
    codec = OpenAIChatCodec(response_id="r", model="public", include_usage=True)
    wire = b"".join(codec.encode_event(InferenceEvent(kind="started")))
    assert b'"role":"assistant"' in wire
    wire = b"".join(codec.encode_event(InferenceEvent(kind="finished", finish_reason="stop", usage=Usage(4, 5))))
    assert b'"prompt_tokens":4' in wire
    assert wire.endswith(b"data: [DONE]\n\n")


@pytest.mark.parametrize("status", [401, 429, 500])
@pytest.mark.asyncio
async def test_adapter_rejection_is_not_a_success_or_hidden_retry(status):
    from autobuild_json.gateway.protocols.openai_chat import OpenAIChatCodec
    from autobuild_json.gateway.providers.openai import OpenAIAdapter
    from autobuild_json.gateway.transport.http import Transport
    from autobuild_json.gateway.transport.egress import EgressPolicy
    from autobuild_json.gateway.errors import GatewayError
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "secret-provider-message"}})
    async def resolver(host, port):
        return ["93.184.216.34"]
    async def secret(route):
        return "synthetic"
    adapter = OpenAIAdapter(Transport(EgressPolicy(resolver=resolver), adapter=httpx.MockTransport(respond)), secret)
    request = OpenAIChatCodec().decode({"model": "m", "messages": []}, {})
    with pytest.raises(GatewayError) as exc:
        async with adapter.open(request, SimpleNamespace(root="https://provider.invalid", upstream_model="m", timeout=10),
                                SimpleNamespace(proxy=None, deadline=datetime.now(timezone.utc)+timedelta(seconds=10))):
            pass
    assert len(calls) == 1
    assert "secret-provider-message" not in str(exc.value)


@pytest.mark.asyncio
async def test_truncated_stream_and_missing_usage_are_distinct():
    from autobuild_json.gateway.providers.openai import chat_events
    from autobuild_json.gateway.errors import GatewayError
    class Response:
        def __init__(self, complete):
            self.complete = complete
        async def chunks(self):
            yield b'data: {"choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":"stop"}]}\n\n'
            if self.complete:
                yield b'data: [DONE]\n\n'
    events = [event async for event in chat_events(Response(True))]
    assert events[-1].kind == "finished" and events[-1].usage is None
    with pytest.raises(GatewayError, match="upstream_error"):
        _ = [event async for event in chat_events(Response(False))]
