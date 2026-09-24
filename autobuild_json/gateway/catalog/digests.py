"""Stable digests for catalog content and idempotent operations."""

from decimal import Decimal
import hashlib
import json
from typing import Any

from .records import CatalogEntry, OperationRequest


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non_finite_decimal")
        return str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def content_digest(entries: tuple[CatalogEntry, ...]) -> str:
    content = [
        {
            "upstream_id": entry.upstream_id,
            "metadata": {
                key: {"value": observed.value, "source": observed.source, "path": observed.path}
                for key, observed in entry.metadata.items()
            },
        }
        for entry in entries
    ]
    content.sort(key=canonical_bytes)
    return hashlib.sha256(canonical_bytes(content)).hexdigest()


def operation_digest(request: OperationRequest) -> bytes:
    payload = request.model_dump(mode="json", exclude={"id"})
    return hashlib.sha256(canonical_bytes(payload)).digest()
