from dataclasses import dataclass, field
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator

Protocol = Literal["openai", "anthropic", "gemini", "ollama"]
Quota = Annotated[int, Field(strict=True, ge=0, lt=10**38)]
ModelId = Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:-]*$")]


class ModelRate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model_id: ModelId
    input_micro: Quota
    output_micro: Quota


class KeyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model_ids: frozenset[ModelId] = frozenset()
    all_models: bool = False
    protocols: frozenset[Protocol] = frozenset()
    total_micro: Quota | None = 0
    day_micro: Quota | None = None
    month_micro: Quota | None = None
    model_overrides: tuple[ModelRate, ...] = ()
    rpm: int = Field(default=60, strict=True, ge=1, le=100_000)
    concurrency: int = Field(default=1, strict=True, ge=1, le=1_000)
    expires_at: AwareDatetime | None = None
    enabled: bool = True

    @field_validator("model_ids")
    @classmethod
    def bounded_models(cls, value):
        if len(value) > 1000:
            raise ValueError("Too many allowed models")
        return value

    @field_validator("model_overrides")
    @classmethod
    def unique_overrides(cls, value):
        if len(value) > 1000 or len({item.model_id for item in value}) != len(value):
            raise ValueError("Duplicate or excessive model overrides")
        return value


@dataclass(frozen=True)
class Principal:
    customer_id: UUID
    key_id: UUID
    policy_version: int


@dataclass(frozen=True)
class IssuedKey:
    key_id: UUID
    secret: str = field(repr=False)
    version: int


class KeyView(BaseModel):
    model_config = ConfigDict(frozen=True)
    key_id: UUID
    customer_id: UUID
    prefix: str
    name: str
    policy: KeyPolicy
    version: int
    revoked_at: AwareDatetime | None
