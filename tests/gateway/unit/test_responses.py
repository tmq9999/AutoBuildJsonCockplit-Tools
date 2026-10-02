import pytest


@pytest.mark.parametrize("extra", [{"background": True}, {"store": True}, {"unknown": True}])
def test_responses_rejects_unsupported_options(extra):
    from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec
    from autobuild_json.gateway.errors import GatewayError
    with pytest.raises(GatewayError, match="unsupported_feature"):
        ResponsesCodec().decode(dict({"model": "m", "input": "hi"}, **extra), {})


def test_responses_tool_calls_round_trip():
    from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec
    body = {"model": "m", "input": [
        {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {"type": "function_call", "call_id": "call1", "name": "f", "arguments": '{"x":1}'},
        {"type": "function_call_output", "call_id": "call1", "output": "ok"}],
        "instructions": "rules", "max_output_tokens": 10, "store": False}
    codec = ResponsesCodec()
    request = codec.decode(body, {})
    wire = codec.upstream_body(request, "actual")
    assert wire["input"] == body["input"]
    assert wire["instructions"] == "rules" and wire["model"] == "actual"


def test_responses_stream_uses_semantic_events_not_chat_chunks():
    from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec
    from autobuild_json.gateway.contracts import InferenceEvent, Text
    from autobuild_json.gateway.metering.records import Usage
    codec = ResponsesCodec(response_id="resp_test", model="m")
    events = [InferenceEvent(kind="started"), InferenceEvent(kind="block_started", item_id="x", block=Text(text="")),
              InferenceEvent(kind="text_delta", item_id="x", delta="Hi"), InferenceEvent(kind="block_finished", item_id="x"),
              InferenceEvent(kind="finished", finish_reason="stop", usage=Usage(4, 5))]
    wire = b"".join(frame for event in events for frame in codec.encode_event(event))
    assert b"event: response.created" in wire
    assert b"event: response.output_text.delta" in wire
    assert b"event: response.completed" in wire
    assert b"chat.completion" not in wire and b"[DONE]" not in wire


def test_responses_accepts_chat_messages_alias_for_simple_text():
    from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec

    request = ResponsesCodec().decode({
        "model": "m",
        "messages": [{"role": "user", "content": "Xin chào"}],
    }, {})

    assert request.model == "m"
    assert request.messages[0].role == "user"
    assert request.messages[0].blocks[0].text == "Xin chào"


def test_responses_rejects_conflicting_input_and_messages():
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec

    with pytest.raises(GatewayError, match="invalid_request"):
        ResponsesCodec().decode({
            "model": "m",
            "input": "Xin chào",
            "messages": [{"role": "user", "content": "Xin chào"}],
        }, {})


@pytest.mark.parametrize("messages", [None, "", {}, {"role": "user", "content": "hi"}, 42])
def test_responses_messages_alias_requires_an_array(messages):
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec

    with pytest.raises(GatewayError, match="invalid_request"):
        ResponsesCodec().decode({"model": "m", "messages": messages}, {})


def test_responses_messages_preserves_tool_history_and_native_options():
    from copy import deepcopy
    from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec

    body = {
        "model": "m",
        "messages": [
            {"role": "developer", "content": "Keep it brief"},
            {"role": "user", "content": [{"type": "text", "text": "Weather?"}]},
            {"role": "assistant", "content": "Checking", "tool_calls": [
                {"id": "call1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call1", "content": "Sunny"},
        ],
        "instructions": "Use the tool",
        "tools": [{"type": "function", "name": "lookup", "parameters": {}, "strict": True}],
        "tool_choice": "required",
        "parallel_tool_calls": False,
        "previous_response_id": "resp_previous",
        "reasoning": {"effort": "high", "summary": "auto"},
        "text": {"verbosity": "low"},
        "prompt_cache_key": "fixture-cache",
        "max_output_tokens": 64,
        "stream": True,
        "store": False,
    }
    original = deepcopy(body)
    codec = ResponsesCodec()
    request = codec.decode(body, {})
    wire = codec.upstream_body(request, "actual")

    assert body == original
    assert request.stream is True
    assert wire["input"] == [
        {"role": "developer", "content": [{"type": "input_text", "text": "Keep it brief"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "Weather?"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "Checking"}]},
        {"type": "function_call", "call_id": "call1", "name": "lookup", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call1", "output": "Sunny"},
    ]
    assert wire["instructions"] == "Use the tool"
    assert wire["tools"] == [{"type": "function", "name": "lookup", "description": "",
                             "parameters": {}, "strict": True}]
    assert wire["tool_choice"] == "required"
    assert wire["parallel_tool_calls"] is False
    assert wire["previous_response_id"] == "resp_previous"
    assert wire["reasoning"] == {"effort": "high", "summary": "auto"}
    assert wire["text"] == {"verbosity": "low"}
    assert wire["prompt_cache_key"] == "fixture-cache"
    assert wire["max_output_tokens"] == 64
    assert "messages" not in wire


@pytest.mark.parametrize("extra", [{"store": True}, {"background": True}, {"max_tokens": 8}])
def test_messages_alias_keeps_responses_option_restrictions(extra):
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec

    with pytest.raises(GatewayError, match="unsupported_feature"):
        ResponsesCodec().decode({"model": "m", "messages": [{"role": "user", "content": "Hi"}], **extra}, {})


def test_responses_accepts_live_codex_request_metadata_and_additional_tools():
    from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec
    from autobuild_json.gateway.contracts import Opaque

    body = {
        "model": "gpt-6-astra",
        "input": [
            {"type": "additional_tools", "role": "developer", "id": "item_tools",
             "tools": [{"type": "namespace", "name": "functions", "tools": [
                 {"type": "custom", "name": "exec", "format": {"type": "text"}},
                 {"type": "function", "name": "lookup", "parameters": {}, "strict": True}]}]},
            {"type": "message", "role": "user", "id": "item_user",
             "content": [{"type": "input_text", "text": "hello"}]},
        ],
        "client_metadata": {
            "session_id": "session-id",
            "thread_id": "thread-id",
            "turn_id": "turn-id",
            "x-codex-installation-id": "installation-id",
            "x-codex-window-id": "window-id",
            "x-codex-turn-metadata": "{}",
        },
        "service_tier": "priority",
        "include": ["reasoning.encrypted_content"],
        "parallel_tool_calls": True,
        "prompt_cache_key": "cache-key",
        "reasoning": {"context": "all_turns", "effort": "low"},
        "store": False,
        "stream": True,
        "text": {"verbosity": "medium"},
        "tool_choice": "auto",
    }

    request = ResponsesCodec().decode(body, {})

    assert request.service_tier == "priority"
    assert request.client_metadata["session_id"] == "session-id"
    assert isinstance(request.messages[0].blocks[0], Opaque)
    assert request.messages[0].blocks[0].provider == "codex_oauth"
    assert request.messages[0].blocks[0].payload["type"] == "additional_tools"


@pytest.mark.parametrize("tier", ["priority", "auto", "fast"])
def test_codex_body_preserves_live_metadata_and_normalizes_codex_only_items(tier):
    from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec
    from autobuild_json.gateway.providers.codex import codex_body

    body = {
        "model": "gpt-6-astra",
        "input": [{"type": "additional_tools", "role": "developer", "id": "item_tools",
                   "tools": [{"type": "function", "name": "lookup", "parameters": {}, "strict": True}]},
                  {"type": "message", "role": "user", "content": "hello"}],
        "client_metadata": {"session_id": "session-id"},
        "service_tier": tier,
        "stream": True,
        "store": False,
    }

    request = ResponsesCodec().decode(body, {})
    wire = codex_body(request, "gpt-6-astra")

    assert wire["client_metadata"] == body["client_metadata"]
    assert wire["input"][0]["type"] == "additional_tools"
    # Cockpit accepts the client hint but strips unsupported non-priority tiers
    # before the Codex upstream request.
    if tier == "priority":
        assert wire["service_tier"] == "priority"
    else:
        assert "service_tier" not in wire
