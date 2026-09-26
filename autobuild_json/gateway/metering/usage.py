"""Provider usage normalization into the gateway's canonical accounting record."""

from .records import Usage


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid_usage")
    return value


def _required(payload, name):
    try:
        return _count(payload[name])
    except (KeyError, TypeError):
        raise ValueError("invalid_usage") from None


def _optional(payload, name):
    if name not in payload:
        return 0
    return _count(payload[name])


def _nested_optional(payload, outer, inner):
    if outer not in payload:
        return 0
    details = payload[outer]
    if not isinstance(details, dict):
        raise ValueError("invalid_usage")
    return _optional(details, inner)


def normalize_provider_usage(payload: dict, provider: str) -> Usage:
    """Normalize one of the supported provider usage payloads.

    OpenAI and Responses (including Codex) report inclusive input counts;
    Anthropic reports uncached input plus separate cache buckets.
    """
    if not isinstance(payload, dict):
        raise ValueError("invalid_usage")
    cached_write = 0
    if provider == "openai_chat":
        input_tokens = _required(payload, "prompt_tokens")
        output_tokens = _required(payload, "completion_tokens")
        cached_read = _nested_optional(payload, "prompt_tokens_details", "cached_tokens")
        cached_write = _nested_optional(payload, "prompt_tokens_details", "cache_write_tokens")
        reasoning = _nested_optional(payload, "completion_tokens_details", "reasoning_tokens")
    elif provider in {"openai_responses", "codex"}:
        input_tokens = _required(payload, "input_tokens")
        output_tokens = _required(payload, "output_tokens")
        cached_read = _nested_optional(payload, "input_tokens_details", "cached_tokens")
        cached_write = _nested_optional(payload, "input_tokens_details", "cache_write_tokens")
        reasoning = _nested_optional(payload, "output_tokens_details", "reasoning_tokens")
    elif provider == "anthropic":
        uncached_input = _required(payload, "input_tokens")
        output_tokens = _required(payload, "output_tokens")
        cached_read = _optional(payload, "cache_read_input_tokens")
        cached_write = _optional(payload, "cache_creation_input_tokens")
        input_tokens = uncached_input + cached_read + cached_write
        return Usage(input_tokens, output_tokens, cached_read=cached_read,
                     cached_write=cached_write)
    elif provider == "gemini":
        input_tokens = _required(payload, "promptTokenCount")
        candidates = _required(payload, "candidatesTokenCount")
        reasoning = _optional(payload, "thoughtsTokenCount")
        output_tokens = candidates + reasoning
        cached_read = _optional(payload, "cachedContentTokenCount")
    else:
        raise ValueError("invalid_usage")
    return Usage(input_tokens, output_tokens, cached_read=cached_read, cached_write=cached_write, reasoning=reasoning)
