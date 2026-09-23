import json
import pytest


def test_ndjson_is_single_unicode_record():
    from autobuild_json.gateway.protocols.ollama import ndjson_record
    wire = ndjson_record({"message": {"content": "chào"}, "done": False})
    assert wire.endswith(b"\n") and wire.count(b"\n") == 1
    assert json.loads(wire)["message"]["content"] == "chào"


def test_generate_defaults_streaming_and_maps_output_limit():
    from autobuild_json.gateway.protocols.ollama import OllamaCodec
    request = OllamaCodec(generate=True).decode({"model": "m", "prompt": "hi", "options": {"num_predict": 10}}, {})
    assert request.stream and request.options.max_output_tokens == 10


def test_unsupported_context_is_not_discarded():
    from autobuild_json.gateway.protocols.ollama import OllamaCodec
    from autobuild_json.gateway.errors import GatewayError
    with pytest.raises(GatewayError, match="unsupported_feature"):
        OllamaCodec(generate=True).decode({"model": "m", "prompt": "hi", "context": [1, 2]}, {})


def test_ollama_tool_result_round_trip_uses_name_mapping():
    from autobuild_json.gateway.protocols.ollama import OllamaCodec
    codec = OllamaCodec()
    body = {"model": "m", "messages": [
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "f", "arguments": {"x": 1}}}]},
        {"role": "tool", "tool_name": "f", "content": "ok"}]}
    parsed = codec.decode(body, {})
    assert codec.upstream_body(parsed, "actual")["messages"] == body["messages"]
