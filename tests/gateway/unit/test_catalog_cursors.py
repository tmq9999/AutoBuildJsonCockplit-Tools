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


def test_cursor_rejects_noncanonical_signature_padding_bits():
    from autobuild_json.gateway.catalog.cursors import decode_cursor, encode_cursor
    from autobuild_json.gateway.errors import GatewayError
    payload = {"v": 1, "source": "00000000-0000-0000-0000-000000000001",
               "run": "00000000-0000-0000-0000-000000000002", "previous": None,
               "after": "model-a", "filter": "changed", "exp": 2000000000}
    token = encode_cursor(payload, b"k" * 32)
    assert decode_cursor(token, b"k" * 32, 1900000000) == payload
    # SHA256's 32-byte digest has two unused bits in its last base64 character.
    # Changing only these bits is a different wire token with identical bytes.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = alphabet.index(token[-1])
    for offset in (1, 2, 3):
        with pytest.raises(GatewayError, match="invalid_request"):
            decode_cursor(token[:-1] + alphabet[last + offset], b"k" * 32, 1900000000)


def test_cursor_rejects_oversized_after_id():
    from autobuild_json.gateway.catalog.cursors import encode_cursor
    from autobuild_json.gateway.errors import GatewayError

    payload = _payload()
    payload["after"] = "x" * 201
    with pytest.raises(GatewayError):
        encode_cursor(payload, b"k" * 32)


def test_cursor_wire_limit_is_2048_characters():
    import base64
    import hashlib
    import hmac
    import json
    from autobuild_json.gateway.catalog.cursors import decode_cursor
    from autobuild_json.gateway.errors import GatewayError

    payload = _payload()
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    def token_for(length):
        target_body = length - 1 - 43
        body = base64.urlsafe_b64encode(raw + b" " * ((target_body * 3 // 4) - len(raw))).rstrip(b"=").decode()
        return body + "." + base64.urlsafe_b64encode(hmac.new(b"k" * 32, body.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    token = token_for(2048)
    assert len(token) == 2048
    assert decode_cursor(token, b"k" * 32, int(time.time()))["after"] == "model-a"
    # A canonical base64 body cannot have length 1 modulo 4, so the first
    # valid signed wire size above 2048 is 2050. Test the literal 2049 boundary
    # as well, without relying solely on invalid signature rejection.
    oversized = token_for(2050)
    assert len(oversized) == 2050
    with pytest.raises(GatewayError, match="invalid_request"):
        decode_cursor(oversized, b"k" * 32, int(time.time()))
    with pytest.raises(GatewayError, match="invalid_request"):
        decode_cursor(token + "A", b"k" * 32, int(time.time()))


def test_snapshot_page_cursor_limit_accepts_2048_rejects_2049():
    from pydantic import ValidationError
    from autobuild_json.gateway.catalog.records import SnapshotPage

    assert SnapshotPage(run_id=uuid4(), next_cursor="a" * 2048).next_cursor == "a" * 2048
    with pytest.raises(ValidationError):
        SnapshotPage(run_id=uuid4(), next_cursor="a" * 2049)


def test_snapshot_stamp_accepts_json_uuid_proxy_values():
    from uuid import uuid4
    from autobuild_json.gateway.catalog.snapshots import _stamp

    proxy = uuid4()
    stamp = _stamp({"source_version": 1, "provider_version": 1, "credential_version": 1,
                    "proxy_id": str(proxy), "proxy_version": 1,
                    "content_digest": "a" * 64, "binding_id": None, "binding_version": None,
                    "budget_id": None, "budget_version": None})
    assert stamp.proxy_id == proxy
