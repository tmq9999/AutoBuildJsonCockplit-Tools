from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@asynccontextmanager
async def empty_clone(source):
    from pydantic import SecretStr
    from sqlalchemy import text
    from autobuild_json.gateway.storage.db import make_database
    from autobuild_json.gateway.storage.migrate import upgrade
    schema = "abgw_test_"+uuid4().hex
    async with source.engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    clone = make_database(SecretStr(source.engine.url.render_as_string(hide_password=False)), schema=schema)
    try:
        await upgrade(clone)
        yield clone
    finally:
        await clone.close()
        async with source.engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


async def test_encrypted_backup_restores_only_to_empty_matching_target(pg_db, tmp_path):
    from sqlalchemy import text
    from autobuild_json.gateway.storage.backup import BackupService
    from autobuild_json.gateway.secrets import Vault
    from autobuild_json.gateway.identity.service import IdentityService
    from autobuild_json.gateway.errors import GatewayError
    vault = Vault({"v1": b"a"*32, "client_keys": b"p"*32}, active="v1")
    await IdentityService(pg_db, b"p"*32).create_customer("private customer marker")
    path = tmp_path/"backup.enc"
    await BackupService(pg_db, vault).export(path)
    assert b"private customer marker" not in path.read_bytes()
    with pytest.raises(GatewayError):
        await BackupService(pg_db, vault).restore(path)
    async with empty_clone(pg_db) as clone:
        await BackupService(clone, vault).restore(path)
        async with clone.sessions() as session:
            assert await session.scalar(text("SELECT name FROM customers")) == "private customer marker"


async def test_missing_backup_key_and_modified_ciphertext_fail_closed(pg_db, tmp_path):
    from autobuild_json.gateway.storage.backup import BackupService
    from autobuild_json.gateway.secrets import Vault
    from autobuild_json.gateway.errors import GatewayError
    source_vault = Vault({"v1": b"a"*32, "client_keys": b"p"*32}, active="v1")
    path = tmp_path/"backup.enc"
    await BackupService(pg_db, source_vault).export(path)
    async with empty_clone(pg_db) as clone:
        wrong = Vault({"v2": b"b"*32, "client_keys": b"p"*32}, active="v2")
        with pytest.raises(GatewayError):
            await BackupService(clone, wrong).restore(path)
        data = bytearray(path.read_bytes())
        data[-5] ^= 1
        path.write_bytes(data)
        with pytest.raises(GatewayError):
            await BackupService(clone, source_vault).restore(path)
