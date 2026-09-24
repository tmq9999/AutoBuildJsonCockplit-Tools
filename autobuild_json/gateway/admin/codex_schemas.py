"""Strict private command DTOs; domain records retain strict scalar rules."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BeforeValidator, Field, StrictBool, field_validator

from ..routing.pool_records import AccountPoolPolicy
from .catalog_schemas import _domain
from .schemas import StrictInput, VersionInput


class AccountUpdate(VersionInput):
    enabled: StrictBool
    proxy_profile_id: UUID | None


class RefreshInput(StrictInput):
    pass


class QuotaAdjustInput(VersionInput):
    request_id: UUID
    amount_micro: int = Field(strict=True, ge=0, lt=10**38)
    reason: str = Field(min_length=1, max_length=500)


class ResetConsumeInput(StrictInput):
    request_id: UUID
    credits_version: int = Field(strict=True, ge=1)
    acknowledge: Literal[True]

    @field_validator("acknowledge", mode="before")
    @classmethod
    def literal_true(cls, value):
        if value is not True:
            raise ValueError("acknowledgement_required")
        return value


class ResetResolveInput(VersionInput):
    outcome: Literal["confirmed_applied", "confirmed_not_applied"]
    reason: str = Field(min_length=1, max_length=500)


PoolWire = Annotated[AccountPoolPolicy, BeforeValidator(lambda value: _domain(AccountPoolPolicy, value))]


class PoolInput(StrictInput):
    version: int = Field(strict=True, ge=0)
    policy: PoolWire
