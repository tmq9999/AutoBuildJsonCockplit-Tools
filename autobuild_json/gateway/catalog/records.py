"""Validated, secret-free records shared by catalog operations."""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from ..identity.policy import ModelId
from ..metering.costs import CostSchedule
from ..metering.budgets import money
from ..errors import GatewayError
from ..metering.records import Bounds, Usage
from ..routing.records import Adapter, ModelConfig, ProviderConfig


class CatalogRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DiscoveryMode(str, Enum):
    openai_single = "openai_single"
    openai_cursor = "openai_cursor"
    anthropic = "anthropic"
    gemini = "gemini"
    ollama = "ollama"


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _decimal(value: Decimal) -> Decimal:
    try:
        return money(value)
    except GatewayError:
        raise ValueError("invalid_decimal_amount") from None


def _json_copy(value: Any) -> Any:
    # The round trip rejects non-JSON values and NaN, and detaches caller-owned containers.
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


class SourceInput(CatalogRecord):
    provider_id: UUID
    credential_id: UUID
    mode: DiscoveryMode
    enabled: bool = True
    schedule_enabled: bool = False
    interval_seconds: int = Field(default=86400, strict=True, ge=300, le=604800)

    @field_validator("mode", mode="before")
    @classmethod
    def parse_mode(cls, value: Any) -> DiscoveryMode:
        return DiscoveryMode(value)


class SourceView(SourceInput):
    id: UUID
    version: int = Field(strict=True, ge=1)
    next_run_at: AwareDatetime | None = None
    latest_successful_run_id: UUID | None = None
    previous_successful_run_id: UUID | None = None

    @field_validator("next_run_at")
    @classmethod
    def normalize_next_run(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None


class ConfigStamp(CatalogRecord):
    source_version: int = Field(strict=True, ge=1)
    provider_version: int = Field(strict=True, ge=1)
    credential_version: int = Field(strict=True, ge=1)
    proxy_id: UUID | None = None
    proxy_version: int | None = Field(default=None, strict=True, ge=1)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_id: UUID | None = None
    binding_version: int | None = Field(default=None, strict=True, ge=1)
    budget_id: UUID | None = None
    budget_version: int | None = Field(default=None, strict=True, ge=1)

    @model_validator(mode="after")
    def paired_versions(self) -> "ConfigStamp":
        for name in ("proxy", "binding", "budget"):
            if (getattr(self, f"{name}_id") is None) != (getattr(self, f"{name}_version") is None):
                raise ValueError(f"{name}_version_pair_required")
        return self


class OperationRoute(CatalogRecord):
    provider_id: UUID
    credential_id: UUID
    root: str = Field(min_length=1, repr=False)
    adapter: Adapter
    wire_api: Literal["chat", "responses"]
    auth_mode: Literal["bearer", "x-api-key", "x-goog-api-key", "none", "oauth"]
    config_version: int = Field(strict=True, ge=1)
    proxy_profile_id: UUID | None = None
    timeout: int = Field(strict=True, ge=1, le=600)

    @field_validator("root")
    @classmethod
    def valid_root(cls, value: str) -> str:
        return ProviderConfig.api_root(value)


class SourceContext(CatalogRecord):
    source: SourceView
    stamp: ConfigStamp
    provider: ProviderConfig
    route: OperationRoute
    effective_proxy_id: UUID | None = None


class ProbeIntent(CatalogRecord):
    binding_id: UUID
    quote_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected: ConfigStamp
    max_hold: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    acknowledged: Literal[True]

    @field_validator("max_hold")
    @classmethod
    def bounded_hold(cls, value: Decimal) -> Decimal:
        return _decimal(value)

    @field_validator("acknowledged", mode="before")
    @classmethod
    def explicit_acknowledgement(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("probe_acknowledgement_required")
        return value


class OperationRequest(CatalogRecord):
    id: UUID
    source_id: UUID
    kind: Literal["discover", "check", "probe"]
    expected: ConfigStamp
    probe: ProbeIntent | None = None

    @model_validator(mode="after")
    def matching_probe(self) -> "OperationRequest":
        if (self.kind == "probe") != (self.probe is not None):
            raise ValueError("probe_intent_required_only_for_probe")
        if self.probe is not None and self.probe.expected != self.expected:
            raise ValueError("probe_expected_stamp_mismatch")
        return self


class OperationView(CatalogRecord):
    id: UUID
    source_id: UUID
    kind: Literal["discover", "check", "probe"]
    state: Literal["queued", "running", "succeeded", "failed", "cancelled", "stale", "usage_pending"]
    version: int = Field(strict=True, ge=1)
    created_at: AwareDatetime
    deadline: AwareDatetime | None = None
    cancel_requested: bool = False
    dispatched_at: AwareDatetime | None = None
    usage: Usage | None = None
    cost: Decimal | None = None
    result: dict[str, Any] = Field(default_factory=dict, repr=False)
    error: str | None = None

    @field_validator("created_at", "deadline", "dispatched_at")
    @classmethod
    def normalize_datetime(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None

    @field_validator("cost")
    @classmethod
    def bounded_cost(cls, value: Decimal | None) -> Decimal | None:
        return _decimal(value) if value is not None else None

    @field_validator("result")
    @classmethod
    def copy_result(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_copy(value)


class Claim(CatalogRecord):
    operation_id: UUID
    source_id: UUID
    owner: UUID
    generation: int = Field(strict=True, ge=1)
    deadline: AwareDatetime

    @field_validator("deadline")
    @classmethod
    def normalize_deadline(cls, value: datetime) -> datetime:
        return _utc(value)


class ObservedValue(CatalogRecord):
    value: Any = Field(default=None, repr=False)
    source: Literal["upstream"] = "upstream"
    path: str | None = None
    observed_at: AwareDatetime

    @field_validator("value")
    @classmethod
    def copy_json(cls, value: Any) -> Any:
        return _json_copy(value)

    @field_validator("observed_at")
    @classmethod
    def normalize_observed(cls, value: datetime) -> datetime:
        return _utc(value)


class CatalogEntry(CatalogRecord):
    upstream_id: str = Field(min_length=1, max_length=200)
    metadata: dict[str, ObservedValue] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def copy_metadata(cls, value: dict[str, ObservedValue]) -> dict[str, ObservedValue]:
        return deepcopy(value)


class SnapshotItem(CatalogRecord):
    upstream_id: str = Field(min_length=1, max_length=200)
    entry: CatalogEntry
    diff: Literal["added", "changed", "missing", "unchanged"]
    decision: dict[str, Any] | None = None

    @field_validator("decision")
    @classmethod
    def copy_decision(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _json_copy(value) if value is not None else None


class SnapshotPage(CatalogRecord):
    run_id: UUID
    previous_run_id: UUID | None = None
    source_changed: bool = False
    items: tuple[SnapshotItem, ...] = Field(default=(), max_length=10000)
    next_cursor: str | None = Field(default=None, max_length=4096)

    @field_validator("next_cursor")
    @classmethod
    def safe_cursor(cls, value: str | None) -> str | None:
        if value is not None and any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("invalid_cursor")
        return value


class ProbeQuote(CatalogRecord):
    stamp: ConfigStamp
    input_bound: int = Field(strict=True, gt=0, lt=2**63)
    output_bound: Literal[16] = 16
    cost_schedule: CostSchedule
    upper_cost: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("upper_cost")
    @classmethod
    def bounded_upper_cost(cls, value: Decimal) -> Decimal:
        return _decimal(value)


class PublishItem(CatalogRecord):
    upstream_id: str = Field(min_length=1, max_length=200)
    public_model_id: ModelId
    identity: str = Field(min_length=1, max_length=200)
    input_bound: int = Field(strict=True, ge=0, lt=2**63)
    output_bound: int = Field(strict=True, ge=0, lt=2**63)
    capabilities: frozenset[str] = Field(min_length=1)
    binding_enabled: bool = False
    new_model: ModelConfig | None = None
    expected_model_version: int | None = Field(default=None, strict=True, ge=1)
    binding_id: UUID | None = None
    binding_version: int | None = Field(default=None, strict=True, ge=1)
    decision_version: int | None = Field(default=None, strict=True, ge=1)

    @model_validator(mode="after")
    def valid_bounds_and_versions(self) -> "PublishItem":
        Bounds(self.input_bound, self.output_bound)
        if (self.binding_id is None) != (self.binding_version is None):
            raise ValueError("binding_version_pair_required")
        if self.new_model is not None and self.new_model.model_id != self.public_model_id:
            raise ValueError("new_model_id_mismatch")
        return self


class PublicationRequest(CatalogRecord):
    id: UUID
    source_id: UUID
    run_id: UUID
    expected: ConfigStamp
    selections: tuple[PublishItem, ...] = Field(min_length=1, max_length=100)

    @field_validator("selections")
    @classmethod
    def unique_selections(cls, value: tuple[PublishItem, ...]) -> tuple[PublishItem, ...]:
        if len({item.upstream_id for item in value}) != len(value):
            raise ValueError("duplicate_upstream_selection")
        if len({item.public_model_id for item in value}) != len(value):
            raise ValueError("duplicate_public_model_selection")
        return value


class DecisionEdit(CatalogRecord):
    upstream_id: str = Field(min_length=1, max_length=200)
    ignored: bool
    expected_version: int | None = Field(default=None, strict=True, ge=1)


class PublishedBinding(CatalogRecord):
    upstream_id: str = Field(min_length=1, max_length=200)
    public_model_id: ModelId
    binding_id: UUID
    model_version: int = Field(strict=True, ge=1)
    binding_version: int = Field(strict=True, ge=1)


class PublicationResult(CatalogRecord):
    operation_id: UUID
    run_id: UUID
    bindings: tuple[PublishedBinding, ...]
