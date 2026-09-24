"""Secret-free, keyed fingerprints of source network identity."""

import hashlib
import hmac

from .digests import canonical_bytes


def context_digest(config: dict, pepper: bytes) -> str:
    """Hash a caller-selected network identity without exposing stored secrets."""
    if not isinstance(pepper, bytes) or len(pepper) < 32:
        raise ValueError("invalid_keyring")
    return hmac.new(pepper, b"abgw:catalog-context:v1:" + canonical_bytes(config), hashlib.sha256).hexdigest()


def storage_fingerprint(value: dict, pepper: bytes) -> str:
    """Fingerprint encrypted storage without decrypting it or exposing its contents."""
    return hmac.new(pepper, b"abgw:catalog-secret:v1:" + canonical_bytes(value), hashlib.sha256).hexdigest()
