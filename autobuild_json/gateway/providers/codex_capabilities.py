CAPABILITIES = {"version": "codex-http-v1", "chat": True, "responses": True,
                "text": True, "tools": True, "compact": False,
                "images": False, "websocket": False}


def codex_capabilities() -> dict:
    return dict(CAPABILITIES)
