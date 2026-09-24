"""Explicit JSON-to-domain conversion without weakening strict catalog records."""

import json
from decimal import Decimal, InvalidOperation
from typing import Annotated
from uuid import UUID

from pydantic import BeforeValidator, Field, StrictBool

from ..catalog.records import ConfigStamp, ProbeIntent, PublishItem, SourceInput, DiscoveryMode
from .schemas import StrictInput, VersionInput


def _domain(record, value):
    if isinstance(value, record):
        return value
    # JSON validation permits JSON UUID/date/collection representations, while
    # retaining strict scalar validation and domain invariants.
    return record.model_validate_json(json.dumps(value, allow_nan=False))


def _probe(value):
    if not isinstance(value, ProbeIntent) and (
        not isinstance(value, dict) or not isinstance(value.get("max_hold"), str)
    ):
        raise ValueError("decimal_string_required")
    return _domain(ProbeIntent, value)


StampWire = Annotated[ConfigStamp, BeforeValidator(lambda value: _domain(ConfigStamp, value))]
ProbeWire = Annotated[ProbeIntent, BeforeValidator(_probe)]
SelectionWire = Annotated[PublishItem, BeforeValidator(lambda value: _domain(PublishItem, value))]


class SourceCreateInput(StrictInput):
    provider_id: UUID
    credential_id: UUID
    mode: DiscoveryMode
    enabled: StrictBool = True
    schedule_enabled: StrictBool = False
    interval_seconds: int = Field(default=86400, strict=True, ge=300, le=604800)

    def domain(self):
        return SourceInput.model_validate({name: getattr(self, name) for name in SourceInput.model_fields})


class SourceUpdateInput(SourceCreateInput):
    version: int = Field(strict=True, ge=1)


class EnqueueInput(StrictInput):
    operation_id: UUID
    expected: StampWire


class ProbeEnqueueInput(EnqueueInput):
    probe: ProbeWire


class DecisionInput(StrictInput):
    upstream_id: str = Field(min_length=1, max_length=200)
    ignored: StrictBool
    expected_version: int | None = Field(default=None, strict=True, ge=1)


class DecisionsInput(VersionInput):
    decisions: list[DecisionInput] = Field(min_length=1, max_length=100)


class ReconcileInput(VersionInput):
    actual: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    reason: str = Field(min_length=1, max_length=500)

    def amount(self):
        from ..metering.budgets import money
        from ..errors import GatewayError
        try:
            return money(Decimal(self.actual))
        except (InvalidOperation, GatewayError):
            raise ValueError("invalid_decimal_amount") from None


class PublishInput(StrictInput):
    operation_id: UUID
    run_id: UUID
    expected: StampWire
    selections: list[SelectionWire] = Field(min_length=1, max_length=100)


class LegacyDiscoverInput(StrictInput):
    pass
