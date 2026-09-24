"""One-way client credentials and record-bound encryption for upstream secrets."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_FORMAT = re.compile(r"abgw_[a-f0-9]{12}\.[A-Za-z0-9_-]{43}\Z")
KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def issue_key() -> tuple[str, str]:
    prefix = "abgw_" + secrets.token_hex(6)
    return prefix, prefix + "." + secrets.token_urlsafe(32)


def key_digest(secret: str, pepper: bytes) -> bytes:
    if not isinstance(secret, str) or not KEY_FORMAT.fullmatch(secret):
        raise ValueError("invalid_api_key")
    if not isinstance(pepper, bytes) or len(pepper) < 32:
        raise ValueError("invalid_keyring")
    return hmac.new(pepper, b"abgw:client-key:v1:" + secret.encode("ascii"), hashlib.sha256).digest()


@dataclass(frozen=True)
class Ciphertext:
    key_id: str
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)

    def to_dict(self):
        return {"key_id": self.key_id, "nonce": base64.b64encode(self.nonce).decode("ascii"),
                "ciphertext": base64.b64encode(self.ciphertext).decode("ascii")}

    @classmethod
    def from_dict(cls, value):
        try:
            if set(value) != {"key_id", "nonce", "ciphertext"} or not KEY_ID.fullmatch(value["key_id"]):
                raise ValueError()
            nonce = base64.b64decode(value["nonce"], validate=True)
            cipher = base64.b64decode(value["ciphertext"], validate=True)
            if len(nonce) != 12 or not 16 <= len(cipher) <= 1_048_592:
                raise ValueError()
            return cls(value["key_id"], nonce, cipher)
        except (ValueError, TypeError, KeyError):
            raise ValueError("secret_unavailable") from None


class Vault:
    def __init__(self, keys: dict[str, bytes], *, active: str):
        if (not keys or active not in keys or any(not isinstance(k, str) or not KEY_ID.fullmatch(k)
                or not isinstance(v, bytes) or len(v) != 32 for k, v in keys.items())):
            raise ValueError("invalid_keyring")
        self._keys = dict(keys)
        self.active = active

    def derive_key(self, key_id, purpose):
        if key_id not in self._keys or purpose not in {"client-key-hmac", "backup-v1", "catalog-cursor-v1"}:
            raise ValueError("invalid_keyring")
        return hmac.new(self._keys[key_id], ("abgw:derive:"+purpose).encode(), hashlib.sha256).digest()

    @staticmethod
    def _aad(kind, record_id):
        if not isinstance(kind, str) or not KEY_ID.fullmatch(kind) or not isinstance(record_id, UUID):
            raise ValueError("secret_unavailable")
        return f"abgw:v1:{kind}:{record_id}".encode("ascii")

    def seal(self, kind: str, record_id: UUID, value: bytes) -> Ciphertext:
        if not isinstance(value, bytes) or len(value) > 1_048_576:
            raise ValueError("secret_unavailable")
        nonce = secrets.token_bytes(12)
        cipher = AESGCM(self._keys[self.active]).encrypt(nonce, value, self._aad(kind, record_id))
        return Ciphertext(self.active, nonce, cipher)

    def open(self, kind: str, record_id: UUID, value: Ciphertext) -> bytes:
        try:
            if len(value.nonce) != 12:
                raise ValueError()
            return AESGCM(self._keys[value.key_id]).decrypt(value.nonce, value.ciphertext, self._aad(kind, record_id))
        except (InvalidTag, KeyError, TypeError, ValueError, AttributeError):
            raise ValueError("secret_unavailable") from None

    @classmethod
    def from_file(cls, path: Path):
        try:
            if path.is_symlink():
                raise ValueError()
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 65_536:
                    raise ValueError()
                if os.name == "posix" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
                    raise ValueError()
                payload = json.loads(stream.read(65_537))
            if set(payload) != {"active", "keys"}:
                raise ValueError()
            return cls({name: base64.b64decode(value, validate=True) for name, value in payload["keys"].items()},
                       active=payload["active"])
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise ValueError("invalid_keyring") from None
