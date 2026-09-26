CAPABILITIES = {"chat": True, "responses": True, "text": True, "tools": True,
                        "version": "codex-http-v2", "compact": True,
                        "images": True, "websocket": True,
                        "image_models": ["gpt-image-2.5", "gpt-image-2"],
                        "websocket_beta": "responses_websockets=2026-02-06"}


def codex_capabilities() -> dict:
    return {key: list(value) if isinstance(value, list) else value for key, value in CAPABILITIES.items()}


def codex_runtime_capabilities() -> dict:
    return codex_capabilities()
