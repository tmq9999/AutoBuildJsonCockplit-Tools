from dataclasses import dataclass, field
import hashlib
import hmac
from uuid import UUID

from ...models import ProxyConfig


@dataclass(frozen=True)
class KiotKey:
    fingerprint: str
    secret: str = field(repr=False)


def parse_kiot_keys(text, *, pepper):
    if len(text) > 5 * 1024 * 1024 or len(text.splitlines()) > 10000 or len(pepper) < 32:
        raise ValueError("invalid_proxy_keys")
    result, seen = [], set()
    for value in text.removeprefix("\ufeff").splitlines():
        value = value.strip()
        if not value:
            continue
        if len(value) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError("invalid_proxy_keys")
        fingerprint = hmac.new(pepper, b"abgw:kiot:v1:" + value.encode(), hashlib.sha256).hexdigest()
        if fingerprint not in seen:
            seen.add(fingerprint)
            result.append(KiotKey(fingerprint, value))
    return result


@dataclass(frozen=True)
class ProxySelection:
    mode: str
    profile_id: UUID | None = None
    runtime_entries: tuple[ProxyConfig | KiotKey, ...] = field(default=(), repr=False)
    region: str = "random"
    protocol: str = "http"
    rotate: bool = False

    def __post_init__(self):
        if self.mode not in {"direct", "fixed", "pool", "kiotproxy"}:
            raise ValueError("invalid_proxy_mode")
        if self.region not in {"random", "bac", "trung", "nam"} or self.protocol not in {"http", "socks5"}:
            raise ValueError("invalid_proxy_config")
        if self.mode == "direct":
            if self.runtime_entries or self.profile_id is not None:
                raise ValueError("invalid_proxy_config")
        elif not self.runtime_entries:
            raise ValueError("empty_proxy_pool")
        elif self.mode == "fixed" and len(self.runtime_entries) != 1:
            raise ValueError("invalid_proxy_config")
        expected = KiotKey if self.mode == "kiotproxy" else ProxyConfig
        if any(not isinstance(entry, expected) for entry in self.runtime_entries):
            raise ValueError("invalid_proxy_config")
