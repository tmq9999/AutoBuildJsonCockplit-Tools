"""Construct model-list requests without provider-controlled URLs or query keys."""

import json

from ..errors import GatewayError
from ..providers.discovery.pagination import CURSOR_KEYS, PAGE_SIZE, SIZE_KEYS, SUFFIXES, valid_cursor, valid_mode
from .http import OutboundRequest

PAGE_BYTES = 2 * 1024 * 1024
TOTAL_BYTES = 8 * 1024 * 1024


async def read_discovery_json(response, remaining_bytes: int) -> tuple[object, int]:
    """Read one decoded body within both page and operation bounds."""
    cap = min(PAGE_BYTES, remaining_bytes)
    data = bytearray()
    async for chunk in response.chunks():
        if len(data) + len(chunk) > cap:
            raise GatewayError("catalog_limit_exceeded", 502, "upstream")
        data.extend(chunk)

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    def nonfinite(_value):
        raise ValueError("nonfinite")

    try:
        payload = json.loads(data, object_pairs_hook=unique_pairs, parse_constant=nonfinite)
        pending = [(payload, 0)]
        while pending:
            value, depth = pending.pop()
            if isinstance(value, (dict, list)):
                if depth >= 32:
                    raise ValueError("depth")
                pending.extend((child, depth + 1) for child in (
                    value.values() if isinstance(value, dict) else value
                ))
        return payload, len(data)
    except (ValueError, UnicodeError, RecursionError):
        raise GatewayError("upstream_error", 502, "upstream") from None


def discovery_request(mode: str, cursor: str | None, auth: tuple[str, str] | None, deadline: float) -> OutboundRequest:
    mode = valid_mode(mode)
    if cursor is not None:
        valid_cursor(cursor)
        if mode not in CURSOR_KEYS:
            raise ValueError("invalid_cursor")
    query: tuple[tuple[str, str], ...] = ()
    if mode in SIZE_KEYS:
        query = ((SIZE_KEYS[mode], str(PAGE_SIZE)),)
        if cursor is not None:
            query += ((CURSOR_KEYS[mode], cursor),)
    headers = (("anthropic-version", "2023-06-01"),) if mode == "anthropic" else ()
    return OutboundRequest("GET", SUFFIXES[mode], None, auth_header=auth, deadline=deadline,
                           headers=headers, query=query, kind="discovery", discovery_mode=mode)
