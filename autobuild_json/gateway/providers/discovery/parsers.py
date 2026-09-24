"""Parse documented model-list envelopes without guessing prices or capabilities.

Shapes: OpenAI https://developers.openai.com/api/reference/resources/models/methods/list.md;
Anthropic https://platform.claude.com/docs/en/api/models/list;
Gemini https://ai.google.dev/api/models; Ollama https://docs.ollama.com/api/tags.
openai_cursor is a separate relay contract, not an OpenAI pagination promise.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
import unicodedata
from typing import Any

from ...catalog.records import CatalogEntry, ObservedValue
from ...errors import GatewayError
from .pagination import MAX_IDS, valid_cursor, valid_mode
from .records import DiscoveryPage


_CAPABILITIES = frozenset({"text", "tools", "vision", "continuation"})
_MODALITIES = frozenset({"text", "image", "audio", "video"})
_PRICE_UNITS = frozenset({"per_token", "per_million_tokens"})


def _unreadable() -> None:
    raise GatewayError("upstream_error", 502, "upstream")


def _incomplete() -> None:
    raise GatewayError("catalog_incomplete", 502, "upstream")


def _safe_text(value: Any, maximum: int) -> str | None:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)):
        return None
    return value


def _identity(value: Any) -> str:
    text = _safe_text(value, 200)
    if text is None:
        _unreadable()
    return unicodedata.normalize("NFC", text)


def _cursor(value: Any) -> str:
    try:
        return valid_cursor(value)
    except ValueError:
        _incomplete()


def _secret_reflected(payload: Any, secret: str | None) -> bool:
    if not isinstance(secret, str) or not secret:
        return False
    pending = [payload]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if secret in item:
                return True
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _iso_date(value: Any) -> str | None:
    text = _safe_text(value, 64)
    if text is None:
        return None
    try:
        if "T" in text or "t" in text:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
        else:
            date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _created(value: Any) -> int | str | None:
    # Native OpenAI uses Unix seconds; Anthropic uses RFC 3339.
    if type(value) is int and 0 <= value <= 253402300799:
        return value
    return _iso_date(value)


def _positive_limit(value: Any) -> int | None:
    return value if type(value) is int and 0 < value <= 1_000_000_000 else None


def _strings(value: Any, allowed: frozenset[str] | None = None) -> list[str] | None:
    if not isinstance(value, list) or len(value) > 32:
        return None
    if any(_safe_text(item, 100) is None or (allowed is not None and item not in allowed) for item in value):
        return None
    if len(value) != len(set(value)):
        return None
    return value.copy()


def _pricing(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict) or set(value) != {"currency", "unit", "input", "output"}:
        return None
    currency, unit = value["currency"], value["unit"]
    if (not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None
            or not isinstance(unit, str) or unit not in _PRICE_UNITS):
        return None
    amounts: dict[str, str] = {}
    for key in ("input", "output"):
        raw = value[key]
        if type(raw) not in {str, int, float} or len(str(raw)) > 100:
            return None
        try:
            amount = Decimal(str(raw))
        except InvalidOperation:
            return None
        if not amount.is_finite() or amount < 0:
            return None
        amounts[key] = str(amount)
    return {"currency": currency, "unit": unit, **amounts}


def _metadata(mode: str, row: dict[str, Any], raw_id: str, id_path: str,
              observed_at: datetime, warnings: set[str]) -> dict[str, ObservedValue]:
    values = {"id": ObservedValue(value=raw_id, path=id_path, observed_at=observed_at)}

    def add(key: str, path: str, validator) -> None:
        if path not in row or row[path] is None:
            return
        value = validator(row[path])
        if value is None:
            warnings.add("malformed_optional_metadata")
        else:
            values[key] = ObservedValue(value=value, path=path, observed_at=observed_at)

    if mode in {"openai_single", "openai_cursor"}:
        add("name", "name", lambda value: _safe_text(value, 200))
        add("owner", "owned_by", lambda value: _safe_text(value, 200))
        add("created", "created", _created)
        add("shutdown", "shutdown_date", _iso_date)
        if "shutdown_date" in row and row["shutdown_date"] is None:
            values["shutdown"] = ObservedValue(value=None, path="shutdown_date", observed_at=observed_at)
        add("capabilities", "capabilities", lambda value: _strings(value, _CAPABILITIES))
        add("modalities", "modalities", lambda value: _strings(value, _MODALITIES))
        add("pricing", "pricing", _pricing)
    elif mode == "anthropic":
        add("name", "display_name", lambda value: _safe_text(value, 200))
        add("created", "created_at", _iso_date)
        add("input_token_limit", "max_input_tokens", _positive_limit)
        add("output_token_limit", "max_tokens", _positive_limit)
    elif mode == "gemini":
        add("name", "displayName", lambda value: _safe_text(value, 200))
        add("input_token_limit", "inputTokenLimit", _positive_limit)
        add("output_token_limit", "outputTokenLimit", _positive_limit)
        add("supported_generation_methods", "supportedGenerationMethods", _strings)
    elif mode == "ollama":
        add("modified_at", "modified_at", _iso_date)
    return values


def _entries(mode: str, rows: Any, observed_at: datetime, warnings: set[str]) -> tuple[CatalogEntry, ...]:
    if not isinstance(rows, list):
        _unreadable()
    if len(rows) > MAX_IDS:
        _incomplete()
    entries: dict[str, CatalogEntry] = {}
    for row in rows:
        if not isinstance(row, dict):
            _unreadable()
        if mode == "gemini":
            raw_id = _identity(row.get("name"))
            upstream_id = _identity(raw_id.removeprefix("models/"))
            id_path = "name"
        elif mode == "ollama":
            name, model = row.get("name"), row.get("model")
            if name is not None and model is not None and name != model:
                _unreadable()
            id_path = "name" if name is not None else "model"
            raw_id = _identity(name if name is not None else model)
            upstream_id = raw_id
        else:
            raw_id = _identity(row.get("id"))
            upstream_id = raw_id
            id_path = "id"
        entry = CatalogEntry(upstream_id=upstream_id,
                             metadata=_metadata(mode, row, raw_id, id_path, observed_at, warnings))
        previous = entries.get(upstream_id)
        if previous is not None and previous != entry:
            _unreadable()
        entries[upstream_id] = entry
    return tuple(entries.values())


def parse_page(mode: str, payload: object, observed_at: datetime, secret: str | None = None) -> DiscoveryPage:
    """Parse one complete response page; callers enforce cross-page caps and loops."""
    valid_mode(mode)
    if not isinstance(payload, dict) or "error" in payload or _secret_reflected(payload, secret):
        _unreadable()
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
        raise ValueError("invalid_observed_at")
    warnings: set[str] = set()
    if mode in {"openai_single", "openai_cursor", "anthropic"}:
        if mode == "openai_single" and payload.get("object", "list") != "list":
            _unreadable()
        rows = payload.get("data")
    else:
        rows = payload.get("models")
    entries = _entries(mode, rows, observed_at, warnings)
    next_cursor = None
    if mode == "openai_single":
        if any(payload.get(field) for field in ("has_more", "next", "next_page", "nextPageToken")):
            _incomplete()
    elif mode in {"openai_cursor", "anthropic"}:
        has_more = payload.get("has_more")
        if type(has_more) is not bool:
            _unreadable()
        if has_more:
            if not entries:
                _incomplete()
            next_cursor = _cursor(payload.get("last_id"))
    elif mode == "gemini":
        token = payload.get("nextPageToken")
        if token is not None:
            next_cursor = _cursor(token)
    elif any(payload.get(field) for field in ("has_more", "next", "next_page", "nextPageToken")):
        _incomplete()
    return DiscoveryPage(entries, next_cursor, next_cursor is None, tuple(sorted(warnings)))
