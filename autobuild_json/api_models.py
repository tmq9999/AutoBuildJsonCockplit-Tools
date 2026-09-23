from enum import Enum
from typing import Literal
from pydantic import Field, SecretStr, model_validator
from uuid import UUID
from .models import StrictModel


class LoginInput(StrictModel):
    token: SecretStr


class AccountText(StrictModel):
    accounts_text: SecretStr


class JobInput(AccountText):
    proxies_text: SecretStr = SecretStr("")
    mode: Literal["sequential", "parallel"] = "sequential"
    workers: int = Field(default=1, ge=1, le=16, strict=True)
    timeout: float = Field(default=180, ge=1, le=600, allow_inf_nan=False)
    proxy_mode: Literal["legacy", "direct", "fixed", "pool", "kiotproxy", "profile"] = "legacy"
    kiot_keys_text: SecretStr = SecretStr("")
    proxy_region: Literal["random", "bac", "trung", "nam"] = "random"
    proxy_protocol: Literal["http", "socks5"] = "http"
    proxy_profile_id: UUID | None = None

    @model_validator(mode="after")
    def proxy_source_fields(self):
        static = bool(self.proxies_text.get_secret_value().strip())
        keys = bool(self.kiot_keys_text.get_secret_value().strip())
        if self.proxy_mode == "kiotproxy":
            valid = keys and not static and self.proxy_profile_id is None
        elif self.proxy_mode == "profile":
            valid = self.proxy_profile_id is not None and not keys and not static
        elif self.proxy_mode == "direct":
            valid = not keys and not static and self.proxy_profile_id is None
        else:
            valid = not keys and self.proxy_profile_id is None and (self.proxy_mode == "legacy" or static)
        if not valid:
            raise ValueError("Conflicting or missing proxy configuration")
        return self


class LinkInput(StrictModel):
    email: str = Field(min_length=3, max_length=254)
    proxy: SecretStr = SecretStr("")


class CallbackInput(StrictModel):
    session_id: str = Field(min_length=36, max_length=36)
    callback_url: SecretStr


class ExportKind(str, Enum):
    success = "success"
    errors = "errors"
    phone_verify = "phone_verify"
    filtered = "filtered"
    cancelled = "cancelled"
