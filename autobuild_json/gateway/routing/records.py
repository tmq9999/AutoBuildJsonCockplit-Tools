from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote, urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..identity.policy import ModelId, Quota
from ..metering.records import Bounds
from ..metering.costs import CostSchedule

Adapter = Literal["openai_compatible", "anthropic", "gemini", "ollama", "codex_oauth"]


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=200)
    adapter: Adapter
    wire_api: Literal["chat", "responses"] = "chat"
    root: str
    enabled: bool = True
    auth_mode: Literal["bearer", "x-api-key", "x-goog-api-key", "none", "oauth"] = "bearer"
    proxy_profile_id: UUID | None = None
    budget_id: UUID | None = None
    cost_schedule: CostSchedule | None = None
    timeout: int = Field(default=180, strict=True, ge=1, le=600)
    rpm_limit: int = Field(default=600,strict=True,ge=1,le=100000)
    concurrency_limit: int = Field(default=16,strict=True,ge=1,le=1000)

    @field_validator("root")
    @classmethod
    def api_root(cls, value):
        try:
            p = urlsplit(value)
            if (p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password
                    or p.query or p.fragment or "\\" in value or any(ord(c) <= 32 for c in value)
                    or any(part in {".", ".."} for part in unquote(p.path).split("/"))
                    or p.port is not None and not 1 <= p.port <= 65535):
                raise ValueError()
        except ValueError:
            raise ValueError("invalid_provider_root") from None
        return value.rstrip("/")


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model_id: ModelId
    identity: str = Field(min_length=1, max_length=200)
    enabled: bool = False
    router_model: bool = False
    input_micro: Quota = 1_000_000
    output_micro: Quota = 1_000_000


@dataclass(frozen=True)
class BindingConfig:
    id: UUID
    provider_id: UUID
    credential_id: UUID
    public_model_id: str
    upstream_model: str
    identity: str
    input_bound: int
    output_bound: int
    capabilities: frozenset[str] = frozenset({"text", "tools"})
    priority: int = 0
    enabled: bool = True

    def __post_init__(self):
        Bounds(self.input_bound, self.output_bound)
        if not self.upstream_model or len(self.upstream_model) > 200 or self.priority < 0:
            raise ValueError("invalid_binding")


@dataclass(frozen=True)
class RouteSnapshot:
    binding_id: UUID
    provider_id: UUID
    credential_id: UUID
    public_model_id: str
    upstream_model: str
    adapter: str
    root: str
    auth_mode: str
    config_version: int
    capabilities: frozenset[str]
    bounds: Bounds
    proxy_profile_id: UUID | None
    budget_id: UUID | None
    priority: int
    input_micro: int
    output_micro: int
    timeout: int
    cost_schedule: CostSchedule | None = None
    wire_api: str = "chat"
