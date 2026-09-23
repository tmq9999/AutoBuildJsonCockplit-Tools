import pytest


def test_query_allows_sse_but_never_url_keys():
    from autobuild_json.gateway.protocols.gemini import validate_query
    assert validate_query({"alt": "sse"}, True) == "sse"
    with pytest.raises(ValueError):
        validate_query({"key": "secret"}, True)


def test_thoughts_are_added_to_output_not_prompt():
    from autobuild_json.gateway.providers.gemini import normalize_gemini_usage
    usage = normalize_gemini_usage({"promptTokenCount": 100, "candidatesTokenCount": 20,
                                    "thoughtsTokenCount": 10, "cachedContentTokenCount": 50})
    assert (usage.input_tokens, usage.output_tokens, usage.cached_read, usage.reasoning) == (100, 30, 50, 10)


def test_function_response_preserves_call_name():
    from autobuild_json.gateway.protocols.gemini import GeminiCodec
    body = {"contents": [{"role": "model", "parts": [{"functionCall": {"name": "f", "args": {"x": 1}}}]},
                          {"role": "user", "parts": [{"functionResponse": {"name": "f", "response": {"result": "ok"}}}]}]}
    codec = GeminiCodec()
    request = codec.decode(body, {"model": "public", "stream": False})
    assert codec.upstream_body(request, "actual")["contents"] == body["contents"]


@pytest.mark.asyncio
async def test_native_function_call_finishes_as_tool_calls():
    from autobuild_json.gateway.providers.gemini import gemini_events
    class Response:
        async def chunks(self):
            yield b'data: {"candidates":[{"content":{"parts":[{"functionCall":{"name":"f","args":{}}}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":1,"candidatesTokenCount":1}}\n\n'
    events = [e async for e in gemini_events(Response())]
    assert events[-1].finish_reason == "tool_calls"
