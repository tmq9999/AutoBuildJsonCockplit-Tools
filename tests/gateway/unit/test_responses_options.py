import pytest

from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec
from autobuild_json.gateway.providers.codex import codex_body


def test_native_options_and_strict_function_tools_survive_codex_normalization():
    body = {"model": "public", "input": "Hi", "max_output_tokens": 1, "temperature": 0.2, "top_p": 0.8,
            "reasoning": {"effort": "high", "summary": "auto"}, "include": ["reasoning.encrypted_content"],
            "text": {"verbosity": "low", "format": {"type": "json_schema", "name": "answer",
                     "strict": True, "schema": {"type": "object", "properties": {}, "additionalProperties": False}}},
            "prompt_cache_key": "synthetic-cache", "tools": [{"type": "function", "name": "lookup", "strict": True,
                     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}]}
    request = ResponsesCodec().decode(body, {})
    wire = codex_body(request, "actual")
    for field in ("reasoning", "include", "text", "prompt_cache_key"):
        assert wire[field] == body[field]
    assert wire["tools"][0]["strict"] is True
    assert wire["model"] == "actual" and wire["stream"] is True and wire["store"] is False
    assert not set(wire) & {"max_output_tokens", "temperature", "top_p"}
    assert request.options.max_output_tokens == 1  # normalization never mutates the canonical request


@pytest.mark.parametrize("extra", [
    {"reasoning": {"effort": "invalid"}}, {"reasoning": {"budget_tokens": 10}},
    {"include": "reasoning.encrypted_content"}, {"include": ["message.output_text.logprobs"]},
    {"include": ["reasoning.encrypted_content"] * 2}, {"prompt_cache_key": 12},
    {"text": {"verbosity": "maximum"}}, {"text": {"unknown": True}},
    {"text": {"format": {"type": "json_schema", "name": "no schema"}}},
    {"text": {"format": {"type": "json_schema", "name": "x", "schema": {}, "strict": "true"}}},
    {"tools": [{"type": "function", "name": "x", "strict": "true"}]},
])
def test_invalid_native_options_fail_before_dispatch(extra):
    with pytest.raises(GatewayError):
        ResponsesCodec().decode({"model": "m", "input": "hi", **extra}, {})


@pytest.mark.parametrize("extra", [
    {"reasoning": {"effort": "low"}}, {"include": ["reasoning.encrypted_content"]},
    {"text": {"verbosity": "low"}}, {"prompt_cache_key": "cache"},
])
def test_native_options_are_not_silently_dropped_on_other_upstream_codecs(extra):
    from autobuild_json.gateway.protocols.openai_chat import OpenAIChatCodec
    from autobuild_json.gateway.protocols.anthropic import AnthropicCodec
    from autobuild_json.gateway.protocols.gemini import GeminiCodec
    from autobuild_json.gateway.protocols.ollama import OllamaCodec
    request = ResponsesCodec().decode({"model": "m", "input": "hi", **extra}, {})
    for codec in (OpenAIChatCodec(), AnthropicCodec(), GeminiCodec(), OllamaCodec()):
        with pytest.raises(GatewayError, match="unsupported_feature"):
            codec.upstream_body(request, "actual")


def test_strict_function_tools_preserved_only_by_supported_wires():
    from autobuild_json.gateway.protocols.openai_chat import OpenAIChatCodec
    from autobuild_json.gateway.protocols.anthropic import AnthropicCodec
    from autobuild_json.gateway.protocols.gemini import GeminiCodec
    from autobuild_json.gateway.protocols.ollama import OllamaCodec
    request = ResponsesCodec().decode({"model": "m", "input": "hi", "tools": [
        {"type": "function", "name": "x", "strict": False, "parameters": {}}]}, {})
    assert OpenAIChatCodec().upstream_body(request, "actual")["tools"][0]["function"]["strict"] is False
    for codec in (AnthropicCodec(), GeminiCodec(), OllamaCodec()):
        with pytest.raises(GatewayError, match="unsupported_feature"):
            codec.upstream_body(request, "actual")


def test_native_openai_keeps_generation_controls_and_omitted_strict():
    codec = ResponsesCodec()
    request = codec.decode({"model": "m", "input": "hi", "max_output_tokens": 12,
        "temperature": 0.2, "tools": [{"type": "function", "name": "x"}]}, {})
    wire = codec.upstream_body(request, "actual")
    assert wire["max_output_tokens"] == 12 and wire["temperature"] == 0.2
    assert "strict" not in wire["tools"][0]
