import pytest


def test_anthropic_cache_buckets_are_added_once():
    from autobuild_json.gateway.providers.anthropic import normalize_anthropic_usage
    u = normalize_anthropic_usage({"input_tokens": 10, "output_tokens": 20,
                                  "cache_read_input_tokens": 70, "cache_creation_input_tokens": 30})
    assert (u.input_tokens, u.output_tokens) == (110, 20)


def test_anthropic_tools_round_trip():
    from autobuild_json.gateway.protocols.anthropic import AnthropicCodec
    codec = AnthropicCodec()
    body = {"model": "m", "max_tokens": 20, "system": "rules", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call1", "name": "f", "input": {"x": 1}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call1", "content": "ok"}]}],
        "tools": [{"name": "f", "description": "f", "input_schema": {"type": "object"}}]}
    request = codec.decode(body, {})
    outgoing = codec.upstream_body(request, "actual")
    assert outgoing["messages"][1:] == body["messages"][1:]
    assert outgoing["system"] == "rules" and outgoing["max_tokens"] == 20


def test_anthropic_cache_control_is_not_silently_discarded():
    from autobuild_json.gateway.protocols.anthropic import AnthropicCodec
    from autobuild_json.gateway.errors import GatewayError
    with pytest.raises(GatewayError, match="unsupported_feature"):
        AnthropicCodec().decode({"model": "m", "max_tokens": 20, "messages": [{"role": "user", "content": [
            {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}]}, {})
