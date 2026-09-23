import base64
from dataclasses import replace
import json
import os
from uuid import UUID

import pytest


def test_secret_cannot_be_moved_between_records_or_kinds():
    from autobuild_json.gateway.secrets import Vault
    vault = Vault({"v1": bytes(range(32))}, active="v1")
    encrypted = vault.seal("credential", UUID(int=1), b"synthetic-upstream-key")
    assert vault.open("credential", UUID(int=1), encrypted) == b"synthetic-upstream-key"
    for kind, identity in [("credential", UUID(int=2)), ("proxy", UUID(int=1))]:
        with pytest.raises(ValueError, match="secret_unavailable"):
            vault.open(kind, identity, encrypted)
    assert "synthetic-upstream-key" not in repr(vault) + repr(encrypted)


def test_ciphertext_tampering_and_wrong_keys_fail_closed():
    from autobuild_json.gateway.secrets import Vault
    vault = Vault({"v1": b"a" * 32}, active="v1")
    encrypted = vault.seal("credential", UUID(int=1), b"synthetic")
    for modified in [replace(encrypted, key_id="missing"), replace(encrypted, nonce=b"bad"),
                     replace(encrypted, ciphertext=encrypted.ciphertext[:-1] + bytes([encrypted.ciphertext[-1] ^ 1]))]:
        with pytest.raises(ValueError, match="secret_unavailable"):
            vault.open("credential", UUID(int=1), modified)
    with pytest.raises(ValueError, match="secret_unavailable"):
        Vault({"v1": b"b" * 32}, active="v1").open("credential", UUID(int=1), encrypted)


def test_rotation_retains_old_key_and_uses_distinct_nonces():
    from autobuild_json.gateway.secrets import Vault, Ciphertext
    before = Vault({"v1": b"a" * 32}, active="v1")
    old = before.seal("credential", UUID(int=1), b"value")
    after = Vault({"v1": b"a" * 32, "v2": b"b" * 32}, active="v2")
    new = after.seal("credential", UUID(int=1), b"value")
    assert old.nonce != new.nonce
    assert new.key_id == "v2"
    assert after.open("credential", UUID(int=1), Ciphertext.from_dict(old.to_dict())) == b"value"


@pytest.mark.parametrize("keys,active", [({}, "v1"), ({"v1": b"short"}, "v1"), ({"v1": bytes(32)}, "v2")])
def test_vault_rejects_invalid_keyrings(keys, active):
    from autobuild_json.gateway.secrets import Vault
    with pytest.raises(ValueError, match="invalid_keyring"):
        Vault(keys, active=active)


def test_client_key_has_256_random_bits_and_scoped_digest():
    from autobuild_json.gateway.secrets import issue_key, key_digest
    prefix, key = issue_key()
    assert key.startswith(prefix + ".")
    assert len(base64.urlsafe_b64decode(key.split(".")[1] + "=")) == 32
    assert key_digest(key, b"a" * 32) != key_digest(key, b"b" * 32)
    assert len(key_digest(key, b"a" * 32)) == 32
    assert issue_key()[1] != key


@pytest.mark.parametrize("invalid", ["", "sk-user-secret", "abgw_x.☃", "abgw_x.\n", "x" * 5000])
def test_malformed_client_keys_are_not_accepted(invalid):
    from autobuild_json.gateway.secrets import key_digest
    with pytest.raises(ValueError, match="invalid_api_key") as exc:
        key_digest(invalid, b"a" * 32)
    assert str(exc.value) == "invalid_api_key"


def test_vault_file_requires_private_regular_file(tmp_path):
    from autobuild_json.gateway.secrets import Vault
    path = tmp_path / "master.json"
    path.write_text(json.dumps({"active": "v1", "keys": {"v1": base64.b64encode(b"a" * 32).decode()}}))
    path.chmod(0o600)
    vault = Vault.from_file(path)
    sealed = vault.seal("credential", UUID(int=1), b"test")
    assert vault.open("credential", UUID(int=1), sealed) == b"test"
    alias = tmp_path / "link"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="invalid_keyring"):
        Vault.from_file(alias)
    if os.name == "posix":
        path.chmod(0o644)
        with pytest.raises(ValueError, match="invalid_keyring"):
            Vault.from_file(path)


def test_gateway_error_never_reflects_unknown_text():
    from autobuild_json.gateway.errors import GatewayError
    exc = GatewayError("password: synthetic-secret", 502, "https://host/?key=synthetic-secret")
    assert "synthetic-secret" not in str(exc) + repr(exc) + str(exc.to_dict())
    assert exc.code == "internal_error"
