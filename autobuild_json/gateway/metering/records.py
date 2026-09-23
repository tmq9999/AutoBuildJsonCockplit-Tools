from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from ..identity.policy import Principal
from .units import weighted_micro


@dataclass(frozen=True)
class Bounds:
    input_tokens: int
    output_tokens: int

    def __post_init__(self):
        if any(type(v) is not int or not 0 <= v < 2**63 for v in (self.input_tokens, self.output_tokens)):
            raise ValueError("invalid_usage")


@dataclass(frozen=True)
class Usage(Bounds):
    cached_read: int = 0
    cached_write: int = 0
    reasoning: int = 0
    source: str = "provider"

    def __post_init__(self):
        super().__post_init__()
        if (any(type(v) is not int or v < 0 for v in (self.cached_read, self.cached_write, self.reasoning))
                or self.cached_read + self.cached_write > self.input_tokens or self.reasoning > self.output_tokens
                or self.source not in {"provider", "admin_adjusted"}):
            raise ValueError("invalid_usage")


@dataclass(frozen=True)
class Admission:
    request_id: UUID
    principal: Principal
    model_id: str
    bounds: Bounds
    input_micro: int
    output_micro: int
    deadline: datetime
    idempotency_digest: bytes | None = field(default=None, repr=False)
    payload_digest: bytes | None = field(default=None, repr=False)
    protocol: str = "openai"

    def __post_init__(self):
        if self.deadline.tzinfo is None:
            raise ValueError("invalid_deadline")
        weighted_micro(self.bounds.input_tokens, self.bounds.output_tokens, self.input_micro, self.output_micro)
        if self.idempotency_digest is not None and (
            len(self.idempotency_digest) != 32 or self.payload_digest is None or len(self.payload_digest) != 32
        ):
            raise ValueError("invalid_idempotency")


@dataclass(frozen=True)
class Hold:
    request_id: UUID
    key_id: UUID
    amount_micro: int
