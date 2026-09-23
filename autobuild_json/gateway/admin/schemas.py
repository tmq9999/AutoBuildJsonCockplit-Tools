from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from .serialization import exact_input

from ..identity.policy import KeyPolicy


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode='before')
    @classmethod
    def decimals(cls, value):
        return exact_input(value)


class CustomerInput(StrictInput):
    name: str = Field(min_length=1, max_length=200)


class VersionInput(StrictInput):
    version: int = Field(strict=True, ge=1)


class EnabledInput(VersionInput):
    enabled: bool
    name: str | None = Field(default=None, min_length=1, max_length=200)


class KeyInput(StrictInput):
    customer_id: UUID
    policy: KeyPolicy
    name: str = Field(default="", max_length=200)


class KeyUpdate(VersionInput):
    policy: KeyPolicy
    name: str | None = Field(default=None, max_length=200)


class CredentialInput(StrictInput):
    secret: SecretStr


class CredentialRotateInput(CredentialInput):
    version: int = Field(strict=True,ge=1)


class ProxyInput(StrictInput):
    name: str = Field(min_length=1, max_length=200)
    mode: Literal["direct", "fixed", "pool", "kiotproxy"]
    entries_text: SecretStr = SecretStr("")
    region: Literal["random", "bac", "trung", "nam"] = "random"
    protocol: Literal["http", "socks5"] = "http"
    rotate: bool = False


class AdjustmentInput(StrictInput):
    amount_micro: int = Field(strict=True, ge=0, lt=10**38)
    reason: str = Field(min_length=1, max_length=500)


class OAuthImportInput(StrictInput):
    record: dict = Field(repr=False)
    proxy_profile_id: UUID | None = None


class PlaygroundInput(StrictInput):
    client_key: SecretStr
    protocol: Literal['openai','anthropic','gemini','ollama']
    body: dict = Field(repr=False)
