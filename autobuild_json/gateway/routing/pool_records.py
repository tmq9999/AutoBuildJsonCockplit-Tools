"""Validated account-pool policy records."""
from decimal import Decimal
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from .records import RouteSnapshot
from ..accounts.quota_records import CodexQuotaSnapshot


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class PoolMember(_Record):
    credential_id: UUID
    priority: int = Field(default=0, ge=0, le=10000)
    weight: int = Field(default=1, ge=1, le=10000)
    backup: bool = False


class AccountPoolPolicy(_Record):
    mode: Literal["auto", "random", "single", "priority", "weight"] = "auto"
    members: tuple[PoolMember, ...] = Field(default_factory=tuple, max_length=1000)
    single_credential_id: UUID | None = None
    session_affinity: bool = True
    reserve_enabled: bool = False
    min_primary_remaining: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    min_secondary_remaining: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    snapshot_max_age_seconds: int = Field(default=120, ge=30, le=3600)
    retry_limit: int = Field(default=1, ge=0, le=1)
    plan_order: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    prefer_expiring: bool = False

    @model_validator(mode="after")
    def valid_policy(self):
        ids = [member.credential_id for member in self.members]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_pool_member")
        if self.mode == "single":
            if self.single_credential_id is None or self.single_credential_id not in ids:
                raise ValueError("single_credential_required")
        elif self.single_credential_id is not None:
            raise ValueError("single_credential_only")
        if len(set(self.plan_order)) != len(self.plan_order) or any(
                not name or len(name) > 200 or not name.isprintable() for name in self.plan_order):
            raise ValueError("invalid_plan_order")
        return self


class PoolScope(_Record):
    customer_id: UUID
    key_id: UUID
    model_id: str = Field(min_length=1, max_length=200)
    session_digest: bytes | None = None

    @field_validator("session_digest")
    @classmethod
    def digest(cls, value: bytes | None) -> bytes | None:
        if value is not None and len(value) != 32:
            raise ValueError("invalid_session_digest")
        return value


class AccountCandidate(_Record):
    route: RouteSnapshot
    quota: CodexQuotaSnapshot | None = None
    health: str = Field(min_length=1, max_length=32)
    cooldown_until: AwareDatetime | None = None
    subscription_expires_at: AwareDatetime | None = None

    @field_validator("cooldown_until", "subscription_expires_at")
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(timezone.utc) if value else None
