import time
from uuid import uuid4

import pytest


def _payload():
    return {"v": 1, "source": str(uuid4()), "run": str(uuid4()), "previous": None,
            "after": "model-a", "filter": "changed", "exp": int(time.time()) + 900}


def test_cursor_round_trip_and_tamper_rejection():
    from autobuild_json.gateway.catalog.cursors import decode_cursor, encode_cursor
    from autobuild_json.gateway.errors import GatewayError

    token = encode_cursor(_payload(), b"k" * 32)
    assert decode_cursor(token, b"k" * 32, int(time.time()))["after"] == "model-a"
    with pytest.raises(GatewayError) as caught:
        decode_cursor(token[:-1] + ("A" if token[-1] != "A" else "B"), b"k" * 32, int(time.time()))
    assert caught.value.code == "invalid_request"


def test_cursor_rejects_expired_and_unknown_fields():
    from autobuild_json.gateway.catalog.cursors import decode_cursor, encode_cursor
    from autobuild_json.gateway.errors import GatewayError

    payload = _payload()
    payload["exp"] = 1
    with pytest.raises(GatewayError):
        decode_cursor(encode_cursor(payload, b"k" * 32), b"k" * 32, int(time.time()))
    payload = _payload()
    payload["extra"] = True
    with pytest.raises(GatewayError):
        encode_cursor(payload, b"k" * 32)


def test_cursor_rejects_oversized_after_id():
    from autobuild_json.gateway.catalog.cursors import encode_cursor
    from autobuild_json.gateway.errors import GatewayError

    payload = _payload()
    payload["after"] = "x" * 201
    with pytest.raises(GatewayError):
        encode_cursor(payload, b"k" * 32)
