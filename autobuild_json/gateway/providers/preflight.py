from ..errors import GatewayError


def validate_request(request, route):
    from ..protocols.openai_chat import OpenAIChatCodec
    from ..protocols.openai_responses import ResponsesCodec
    from ..protocols.anthropic import AnthropicCodec
    from ..protocols.gemini import GeminiCodec
    from ..protocols.ollama import OllamaCodec
    from .codex import codex_body

    request.validate_provider(route.adapter)
    if route.adapter == "codex_oauth":
        codex_body(request, route.upstream_model)
    else:
        codec = {
            "openai_compatible": ResponsesCodec if route.wire_api == "responses" else OpenAIChatCodec,
            "anthropic": AnthropicCodec,
            "gemini": GeminiCodec,
            "ollama": OllamaCodec,
        }.get(route.adapter)
        if codec is None:
            raise GatewayError("unsupported_feature")
        codec().upstream_body(request, route.upstream_model)


def auth_header(mode, secret):
    headers = {
        "bearer": ("Authorization", "Bearer " + secret),
        "x-api-key": ("x-api-key", secret),
        "x-goog-api-key": ("x-goog-api-key", secret),
        "none": None,
    }
    if mode not in headers:
        raise GatewayError("unsupported_feature")
    return headers[mode]
