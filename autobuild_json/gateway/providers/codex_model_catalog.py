"""Independently maintained reference catalog, not an account entitlement.

Metadata observed in Cockpit's Codex client registry (2026-09-26). Unknown
limits remain null. Publishing is explicit and never overwrites admin pricing.
"""
from copy import deepcopy


def _model(identity, title, context=None, maximum=None, *, vision=True, efforts=4, image=False):
    levels = ["low", "medium", "high", "xhigh", "max", "ultra"][:efforts]
    return {"id": identity, "display_name": title, "source": "reference_catalog",
        "context_window": context, "max_context_window": maximum or context,
        "auto_compact_token_limit": context*9//10 if context else None,
        "input_modalities": ["text", "image"] if vision else ["text"],
        "output_modalities": ["image"] if image else ["text"],
        "reasoning_efforts": [] if image else levels,
        "capabilities": ["text", "vision", "images"] if image else
            ["text", "tools", "compact", "websocket", "continuation"]+(["vision"] if vision else [])}


CODEX_MODEL_CATALOG = (
    _model("gpt-6-astra", "GPT-6 Astra", 256000, efforts=6),
    _model("gpt-6-sol", "GPT-6 Sol", 256000, efforts=6),
    _model("gpt-6-luna", "GPT-6 Luna", 256000, efforts=5),
    _model("gpt-5.6-sol", "GPT-5.6 Sol", 272000, 921000, efforts=6),
    _model("gpt-5.6-terra", "GPT-5.6 Terra", 272000, 921000, efforts=6),
    _model("gpt-5.6-luna", "GPT-5.6 Luna", 272000, 921000, efforts=5),
    _model("gpt-5.5", "GPT-5.5", 272000),
    _model("gpt-5.3-codex", "GPT-5.3 Codex"),
    _model("gpt-5.3-codex-spark", "GPT-5.3 Codex Spark", 128000, vision=False),
    _model("gpt-image-2.5", "GPT Image 2.5", image=True),
    _model("gpt-image-2", "GPT Image 2", image=True),
    _model("gpt-reserve", "GPT Reserve"),
    _model("codex-auto-review", "Codex Auto Review", 272000, 921000, efforts=5),
)


def codex_model_catalog():
    return deepcopy(list(CODEX_MODEL_CATALOG))


def client_model(visible_name):
    item = next((m for m in codex_model_catalog() if visible_name == m["id"]
                 or visible_name.endswith("/"+m["id"])), None)
    result = {"slug": visible_name, "display_name": item["display_name"] if item else visible_name,
              "visibility": "list", "supported_in_api": True}
    if item:
        result.update({k: item[k] for k in ("context_window", "max_context_window",
                       "auto_compact_token_limit", "input_modalities") if item[k] is not None})
        result["supported_reasoning_levels"] = [{"effort": value, "description": value}
                                                 for value in item["reasoning_efforts"]]
        result["default_reasoning_level"] = "medium" if item["reasoning_efforts"] else None
        result["supports_parallel_tool_calls"] = "tools" in item["capabilities"]
    return result
