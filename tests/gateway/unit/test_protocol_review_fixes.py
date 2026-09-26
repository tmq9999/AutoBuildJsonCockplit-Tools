import json

import pytest

from autobuild_json.gateway.contracts import InferenceEvent, InferenceResult, Text, ToolCall, ToolResult
from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.protocols.gemini import GeminiCodec
from autobuild_json.gateway.protocols.ollama import OllamaCodec
from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec


FUNCTION_TOOL = {
    "type": "function",
    "name": "lookup",
    "description": "Look up a record",
    "parameters": {"type": "object", "properties": {"key": {"type": "string"}}},
    "strict": True,
}
IMAGE_TOOL = {"type": "image_generation", "quality": "high", "partial_images": 1}


def result(response_id="resp_done"):
    return InferenceResult(
        id=response_id,
        model="public",
        blocks=(Text(text="done"),),
        finish_reason="stop",
    )


def response_frames(codec):
    events = (
        InferenceEvent(kind="started", response_id="resp_stream"),
        InferenceEvent(kind="block_started", item_id="text", block=Text(text="")),
        InferenceEvent(kind="text_delta", item_id="text", delta="done"),
        InferenceEvent(kind="block_finished", item_id="text"),
        InferenceEvent(kind="finished", finish_reason="stop"),
    )
    wire = b"".join(frame for event in events for frame in codec.encode_event(event)).decode()
    return [json.loads(line[6:]) for line in wire.splitlines() if line.startswith("data: ")]


def test_responses_nonstream_envelope_preserves_decoded_tool_configuration():
    codec = ResponsesCodec()
    codec.decode({
        "model": "public",
        "input": "private prompt",
        "tools": [FUNCTION_TOOL, IMAGE_TOOL],
        "tool_choice": "required",
        "parallel_tool_calls": False,
    }, {})

    encoded = codec.encode_result(result())

    assert encoded["tools"] == [FUNCTION_TOOL, {"type": "image_generation", "action": "auto", **IMAGE_TOOL}]
    assert encoded["tool_choice"] == "required"
    assert encoded["parallel_tool_calls"] is False
    assert "private prompt" not in json.dumps(encoded)


def test_responses_stream_created_and_completed_preserve_tools_without_input():
    codec = ResponsesCodec()
    codec.decode({
        "model": "public",
        "input": "private stream prompt",
        "stream": True,
        "tools": [FUNCTION_TOOL, IMAGE_TOOL],
        "tool_choice": "none",
        "parallel_tool_calls": False,
    }, {})

    frames = response_frames(codec)
    envelopes = [frame["response"] for frame in frames
                 if frame["type"] in {"response.created", "response.completed"}]

    assert len(envelopes) == 2
    for envelope in envelopes:
        assert envelope["tools"] == [FUNCTION_TOOL, {"type": "image_generation", "action": "auto", **IMAGE_TOOL}]
        assert envelope["tool_choice"] == "none"
        assert envelope["parallel_tool_calls"] is False
        assert "private stream prompt" not in json.dumps(envelope)


def test_responses_encoder_without_native_decode_keeps_cross_protocol_defaults():
    encoded = ResponsesCodec(model="public").encode_result(result())

    assert encoded["tools"] == []
    assert encoded["tool_choice"] == "auto"
    assert encoded["parallel_tool_calls"] is True


def gemini_history(responses):
    return {"contents": [
        {"role": "model", "parts": [
            {"functionCall": {"id": "call-a", "name": "lookup", "args": {"key": "a"}}},
            {"functionCall": {"id": "call-b", "name": "lookup", "args": {"key": "b"}}},
        ]},
        {"role": "user", "parts": [{"functionResponse": response} for response in responses]},
    ]}


def flattened_blocks(request):
    return [block for message in request.messages for block in message.blocks]


def test_gemini_name_only_tool_results_consume_repeated_calls_fifo():
    request = GeminiCodec().decode(gemini_history([
        {"name": "lookup", "response": {"value": "a"}},
        {"name": "lookup", "response": {"value": "b"}},
    ]), {"model": "public"})

    blocks = flattened_blocks(request)
    assert [block.call_id for block in blocks if isinstance(block, ToolCall)] == ["call-a", "call-b"]
    assert [block.call_id for block in blocks if isinstance(block, ToolResult)] == ["call-a", "call-b"]


def test_gemini_explicit_result_id_consumes_that_call_before_name_fifo():
    request = GeminiCodec().decode(gemini_history([
        {"id": "call-b", "name": "lookup", "response": {"value": "b"}},
        {"name": "lookup", "response": {"value": "a"}},
    ]), {"model": "public"})

    results = [block for block in flattened_blocks(request) if isinstance(block, ToolResult)]
    assert [block.call_id for block in results] == ["call-b", "call-a"]


@pytest.mark.parametrize("responses", [
    [{"id": "missing", "name": "lookup", "response": {}}],
    [{"id": "call-a", "name": "other", "response": {}}],
    [
        {"name": "lookup", "response": {}},
        {"name": "lookup", "response": {}},
        {"name": "lookup", "response": {}},
    ],
])
def test_gemini_rejects_unknown_mismatched_or_overconsumed_results(responses):
    with pytest.raises(GatewayError, match="invalid_request"):
        GeminiCodec().decode(gemini_history(responses), {"model": "public"})


def ollama_history(results):
    return {"model": "public", "messages": [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call-a", "function": {"name": "lookup", "arguments": {"key": "a"}}},
            {"id": "call-b", "function": {"name": "lookup", "arguments": {"key": "b"}}},
        ]},
        *({"role": "tool", "tool_name": name, "content": value} for name, value in results),
    ]}


def test_ollama_name_only_tool_results_consume_repeated_calls_fifo():
    request = OllamaCodec().decode(ollama_history([
        ("lookup", "a"),
        ("lookup", "b"),
    ]), {})

    blocks = flattened_blocks(request)
    assert [block.call_id for block in blocks if isinstance(block, ToolCall)] == ["call-a", "call-b"]
    assert [block.call_id for block in blocks if isinstance(block, ToolResult)] == ["call-a", "call-b"]


@pytest.mark.parametrize("results", [
    [("unknown", "x")],
    [("lookup", "a"), ("lookup", "b"), ("lookup", "c")],
])
def test_ollama_rejects_unknown_or_overconsumed_name_only_results(results):
    with pytest.raises(GatewayError, match="invalid_request"):
        OllamaCodec().decode(ollama_history(results), {})
