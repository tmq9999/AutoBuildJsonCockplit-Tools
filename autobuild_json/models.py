import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from .errors import FlowError


@dataclass(frozen=True)
class AccountInput:
    account_job_id: str
    line_number: int
    email: str
    password: str = field(repr=False)
    secret: str = field(repr=False)


@dataclass(frozen=True)
class RejectedLine:
    line_number: int
    code: str
    reason: str


@dataclass
class ParseReport:
    accounts: list[AccountInput]
    rejected: list[RejectedLine]
    duplicate_lines: list[int]


@dataclass(frozen=True)
class ProxyConfig:
    server: str
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)

    @property
    def url(self):
        scheme, address = self.server.split("://", 1)
        auth = ""
        if self.username is not None:
            auth = quote(self.username, safe="") + ":" + quote(self.password or "", safe="") + "@"
        return f"{scheme}://{auth}{address}"

    @property
    def key(self):
        return hashlib.sha256(self.url.encode()).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountIdentity(StrictModel):
    id: str = Field(min_length=1)


class TokenSet(StrictModel):
    id_token: str = Field(min_length=1, repr=False)
    access_token: str = Field(min_length=1, repr=False)
    refresh_token: str = Field(min_length=1, repr=False)


class SuccessRecord(StrictModel):
    id: str
    email: str
    account: AccountIdentity
    tokens: TokenSet = Field(repr=False)


@dataclass
class RunResult:
    status: str
    record: SuccessRecord | None = field(default=None, repr=False)
    error: FlowError | None = None
    attempt: int = 1

    def __post_init__(self):
        if self.status not in {"success", "error", "phone_verify", "cancelled"}:
            raise ValueError("Invalid terminal status")
        if (self.status == "success") != (self.record is not None):
            raise ValueError("Success requires a record; other outcomes forbid it")


@dataclass
class AttemptContext:
    deadline: float
    cancel: threading.Event
    on_stage: Callable[[str, int], None]

    def check(self):
        if self.cancel.is_set():
            raise FlowError("CANCELLED")
        if time.monotonic() >= self.deadline:
            raise FlowError("TIMEOUT")

    def remaining_timeout(self):
        self.check()
        return min(30.0, self.deadline - time.monotonic())


Processor = Callable[[AccountInput, ProxyConfig | None, AttemptContext], RunResult]
