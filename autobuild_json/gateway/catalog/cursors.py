"""Signed, bounded cursors for immutable catalog snapshot pages."""

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
from typing import Any
from uuid import UUID

from ..errors import GatewayError

_FIELDS = frozenset({"v", "source", "run", "previous", "after", "filter", "exp"})
_FILTERS = frozenset({None, "added", "changed", "missing", "unchanged"})


def _fail() -> GatewayError:
    return GatewayError("invalid_request", 400)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    if not isinstance(value, str) or not value or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for ch in value):
        raise ValueError
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _b64(decoded) != value:
        raise ValueError
    return decoded


def _validate(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise _fail()
    if type(payload["v"]) is not int or payload["v"] != 1:
        raise _fail()
    for name in ("source", "run"):
        if not isinstance(payload[name], str):
            raise _fail()
        try:
            UUID(payload[name])
        except (ValueError, AttributeError):
            raise _fail() from None
    previous = payload["previous"]
    if previous is not None:
        if not isinstance(previous, str):
            raise _fail()
        try:
            UUID(previous)
        except (ValueError, AttributeError):
            raise _fail() from None
    after = payload["after"]
    if after is not None and (not isinstance(after, str) or not 1 <= len(after) <= 200):
        raise _fail()
    if not (payload["filter"] is None or (isinstance(payload["filter"], str) and payload["filter"] in _FILTERS)):
        raise _fail()
    if type(payload["exp"]) is not int or payload["exp"] <= 0:
        raise _fail()
    return payload


def encode_cursor(payload: dict, key: bytes) -> str:
    if not isinstance(key, bytes) or not key:
        raise _fail()
    payload = _validate(payload)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    body = _b64(raw)
    signature = _b64(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    token = body + "." + signature
    if len(token) > 2048:
        raise _fail()
    return token


def decode_cursor(token: str, key: bytes, now) -> dict:
    if not isinstance(token, str) or len(token) > 2048 or not isinstance(key, bytes) or not key:
        raise _fail()
    try:
        body, supplied = token.split(".", 1)
        expected = hmac.new(key, body.encode("ascii"), hashlib.sha256).digest()
        supplied_bytes = _unb64(supplied)
    except (ValueError, UnicodeEncodeError, TypeError):
        raise _fail() from None
    if not hmac.compare_digest(expected, supplied_bytes):
        raise _fail()
    try:
        payload = _validate(json.loads(_unb64(body).decode("utf-8")))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise _fail() from None
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        epoch = int(now.timestamp())
    else:
        epoch = int(now)
    if payload["exp"] <= epoch:
        raise _fail()
    return payload
