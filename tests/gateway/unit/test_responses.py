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
