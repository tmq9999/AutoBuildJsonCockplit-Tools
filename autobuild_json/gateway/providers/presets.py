"""Local provider setup templates; roots require admin input."""

from copy import deepcopy


_PRESETS = (
    {
        "id": "custom-openai-chat", "version": 1, "description": "OpenAI-compatible chat API",
        "discovery_mode": "openai_single",
        "provider": {"name": "Custom OpenAI Chat", "adapter": "openai_compatible", "wire_api": "chat", "root": "", "auth_mode": "bearer"},
    },
    {
        "id": "custom-openai-responses", "version": 1, "description": "OpenAI-compatible responses API",
        "discovery_mode": "openai_single",
        "provider": {"name": "Custom OpenAI Responses", "adapter": "openai_compatible", "wire_api": "responses", "root": "", "auth_mode": "bearer"},
    },
    {
        "id": "anthropic-native", "version": 1, "description": "Anthropic native API",
        "discovery_mode": "anthropic",
        "provider": {"name": "Anthropic", "adapter": "anthropic", "wire_api": "chat", "root": "", "auth_mode": "x-api-key"},
    },
    {
        "id": "gemini-native", "version": 1, "description": "Gemini native API",
        "discovery_mode": "gemini",
        "provider": {"name": "Gemini", "adapter": "gemini", "wire_api": "chat", "root": "", "auth_mode": "x-goog-api-key"},
    },
    {
        "id": "ollama-native", "version": 1, "description": "Ollama native API",
        "discovery_mode": "ollama",
        "provider": {"name": "Ollama", "adapter": "ollama", "wire_api": "chat", "root": "", "auth_mode": "none"},
    },
)


def list_presets() -> tuple[dict, ...]:
    return deepcopy(_PRESETS)
