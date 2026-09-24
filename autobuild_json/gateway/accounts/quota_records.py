"""Immutable, secret-free Codex account quota and reset records."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from ..errors import SAFE_CODES


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    @field_validator("*", mode="after")
    @classmethod
    def printable(cls, value):
        if isinstance(value, str) and (not value or not value.isprintable()):
            raise ValueError("invalid_text")
        return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_required")
    return value.astimezone(timezone.utc)


class QuotaWindow(_Record):
    used_percent: Decimal | None = None
    window_seconds: int | None = Field(default=None, ge=0)
    reset_at: AwareDatetime | None = None

    @field_validator("used_percent")
    @classmethod
    def percent(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value < 0 or value > 100):
            raise ValueError("invalid_quota_percent")
        return value

    @field_validator("reset_at")
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value else None


class CodexQuotaSnapshot(_Record):
    credential_id: UUID
    version: int = Field(ge=1)
    plan_type: str | None = Field(default=None, max_length=200)
    primary: QuotaWindow | None = None
    secondary: QuotaWindow | None = None
    limit_reached: bool | None = None
    fetched_at: AwareDatetime
    last_error: str | None = Field(default=None, max_length=200)

    @field_validator("last_error")
    @classmethod
    def safe_error(cls, value):
        if value is not None and value not in SAFE_CODES:
            raise ValueError("invalid_error_code")
        return value

    @field_validator("fetched_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return _aware(value)


class ResetCredit(_Record):
    id: str | None = Field(default=None, max_length=200)
    state: Literal["available", "redeemed", "expired", "unknown"]
    expires_at: AwareDatetime | None = None

    @field_validator("expires_at")
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value else None


class ResetCreditsSnapshot(_Record):
    credential_id: UUID
    version: int = Field(ge=1)
    available_count: int | None = Field(default=None, ge=0)
    credits: tuple[ResetCredit, ...] = Field(default_factory=tuple, max_length=1000)
    fetched_at: AwareDatetime

    @field_validator("fetched_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return _aware(value)


class ResetResult(_Record):
    operation_id: UUID
    credential_id: UUID
    version: int = Field(ge=1)
    state: Literal["prepared", "dispatched", "rejected", "unknown", "succeeded", "succeeded_refresh_failed"]
    result_code: str | None = Field(default=None, max_length=64)
    upstream_status: int | None = Field(default=None, ge=100, le=599)

    @field_validator("result_code")
    @classmethod
    def safe_code(cls, value):
        return CodexQuotaSnapshot.safe_error(value)


class ResetClaim(_Record):
    result: ResetResult
    generation: int = Field(ge=1)
    redeem_request_id: UUID
    is_new: bool
    deadline: AwareDatetime

    @field_validator("deadline")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return _aware(value)


class RefreshClaim(_Record):
    credential_id: UUID
    provider_id: UUID
    proxy_id: UUID | None = None
    generation: int = Field(ge=1)
    deadline: AwareDatetime
    provider_version: int = Field(ge=1)
    credential_version: int = Field(ge=1)
    proxy_version: int | None = Field(default=None, ge=1)
    token_generation: int = Field(ge=0)

    @field_validator("deadline")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return _aware(value)
