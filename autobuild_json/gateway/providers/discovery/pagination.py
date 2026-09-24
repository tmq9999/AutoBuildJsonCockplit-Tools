"""Fixed discovery query vocabulary and operation bounds."""

import unicodedata

from ...catalog.records import DiscoveryMode


QUERY_KEYS = {
    "openai_single": frozenset(),
    "openai_cursor": frozenset({"after", "limit"}),
    "anthropic": frozenset({"after_id", "limit"}),
    "gemini": frozenset({"pageToken", "pageSize"}),
    "ollama": frozenset(),
}
SUFFIXES = {
    "openai_single": "models",
    "openai_cursor": "models",
    "anthropic": "models",
    "gemini": "models",
    "ollama": "tags",
}
CURSOR_KEYS = {"openai_cursor": "after", "anthropic": "after_id", "gemini": "pageToken"}
SIZE_KEYS = {"openai_cursor": "limit", "anthropic": "limit", "gemini": "pageSize"}
PAGE_SIZE = 100
MAX_PAGES = 10
MAX_IDS = 10_000
MAX_CURSOR_LENGTH = 2048


def valid_mode(mode: str) -> str:
    if not isinstance(mode, str) or mode not in {item.value for item in DiscoveryMode}:
        raise ValueError("invalid_discovery_mode")
    return mode


def valid_cursor(cursor: str) -> str:
    if (not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_LENGTH
            or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in cursor)):
        raise ValueError("invalid_cursor")
    return cursor


def validate_query(mode: str, query: tuple[tuple[str, str], ...]) -> None:
    valid_mode(mode)
    if not isinstance(query, tuple) or any(not isinstance(pair, tuple) or len(pair) != 2
                                            or any(not isinstance(part, str) for part in pair)
                                            for pair in query):
        raise ValueError("invalid_upstream_query")
    names = [name for name, _ in query]
    if len(names) != len(set(names)) or not set(names) <= QUERY_KEYS[mode]:
        raise ValueError("invalid_upstream_query")
    if mode not in SIZE_KEYS:
        if query:
            raise ValueError("invalid_upstream_query")
        return
    items = dict(query)
    size = items.get(SIZE_KEYS[mode])
    if size is None or len(size) > 4 or not size.isascii() or not size.isdecimal() or not 1 <= int(size) <= 1000:
        raise ValueError("invalid_upstream_query")
    cursor = items.get(CURSOR_KEYS[mode])
    if cursor is not None:
        try:
            valid_cursor(cursor)
        except ValueError:
            raise ValueError("invalid_upstream_query") from None
