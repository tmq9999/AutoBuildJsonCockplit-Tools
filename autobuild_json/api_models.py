from enum import Enum
from typing import Literal
from pydantic import Field, SecretStr
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
