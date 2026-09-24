"""Construct model-list requests without provider-controlled URLs or query keys."""

from ..providers.discovery.pagination import CURSOR_KEYS, PAGE_SIZE, SIZE_KEYS, SUFFIXES, valid_cursor, valid_mode
from .http import OutboundRequest


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
