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


async def test_catalog_backup_restores_circular_pointer_decision_claim_and_probe_hold(pg_db, tmp_path):
    import json
    from decimal import Decimal
    from sqlalchemy import text
    from autobuild_json.gateway.storage.backup import BackupService
    from tests.gateway.catalog_support import source_case, synthetic_vault

    _, source = await source_case(pg_db)
    run_id, probe_id, budget_id, attempt_id, client_id = (uuid4() for _ in range(5))
    async with pg_db.sessions.begin() as session:
        await session.execute(text("INSERT INTO upstream_budgets(id,currency,budget_limit,held) "
                                   "VALUES (:id,'USD',100,2.5)"), {"id": budget_id})
        await session.execute(text("INSERT INTO provider_operations"
                                   "(id,source_id,kind,state,payload_digest,input,expected,queued_expires_at,result) "
                                   "VALUES (:id,:source,'discover','succeeded',:digest,'{}'::jsonb,'{}'::jsonb,"
                                   "clock_timestamp(),'{}'::jsonb)"),
                              {"id": run_id, "source": source.id, "digest": b"r" * 32})
        await session.execute(text("UPDATE catalog_sources SET latest_successful_run_id=:run WHERE id=:source"),
                              {"run": run_id, "source": source.id})
        await session.execute(text("INSERT INTO catalog_entries(run_id,upstream_id,metadata,digest) "
                                   "VALUES (:run,'model-A',CAST(:metadata AS jsonb),:digest)"),
                              {"run": run_id, "metadata": json.dumps({"display_name": "A"}), "digest": b"e" * 32})
        await session.execute(text("INSERT INTO catalog_decisions(source_id,upstream_id,ignored) "
                                   "VALUES (:source,'model-A',true)"), {"source": source.id})
        await session.execute(text("INSERT INTO provider_operations"
                                   "(id,source_id,kind,state,payload_digest,input,expected,queued_expires_at,"
                                   "attempt_id,budget_id,upper_cost,settlement_source,retention_protected) "
                                   "VALUES (:id,:source,'probe','usage_pending',:digest,'{}'::jsonb,'{}'::jsonb,"
                                   "clock_timestamp(),:attempt,:budget,2.5,'unknown',true)"),
                              {"id": probe_id, "source": source.id, "digest": b"p" * 32,
                               "attempt": attempt_id, "budget": budget_id})
        await session.execute(text("INSERT INTO upstream_reservations(attempt_id,budget_id,amount,state) "
                                   "VALUES (:attempt,:budget,2.5,'pending')"),
                              {"attempt": attempt_id, "budget": budget_id})
        await session.execute(text("INSERT INTO provider_operation_claims(client_id,operation_id,payload_digest) "
                                   "VALUES (:client,:operation,:digest)"),
                              {"client": client_id, "operation": probe_id, "digest": b"p" * 32})
        await session.execute(text("INSERT INTO catalog_publications"
                                   "(id,source_id,run_id,payload_digest,result) "
                                   "VALUES (:id,:source,:run,:digest,'{}'::jsonb)"),
                              {"id": uuid4(), "source": source.id, "run": run_id, "digest": b"u" * 32})
    vault = synthetic_vault()
    backup = tmp_path / "catalog.enc"
    await BackupService(pg_db, vault).export(backup)
    async with empty_clone(pg_db) as clone:
        await BackupService(clone, vault).restore(backup)
        async with clone.sessions() as session:
            assert await session.scalar(text("SELECT latest_successful_run_id FROM catalog_sources "
                                             "WHERE id=:source"), {"source": source.id}) == run_id
            assert await session.scalar(text("SELECT ignored FROM catalog_decisions WHERE source_id=:source"),
                                        {"source": source.id}) is True
            assert await session.scalar(text("SELECT operation_id FROM provider_operation_claims "
                                             "WHERE client_id=:client"), {"client": client_id}) == probe_id
            assert await session.scalar(text("SELECT held FROM upstream_budgets WHERE id=:id"),
                                        {"id": budget_id}) == Decimal("2.5")
            assert await session.scalar(text("SELECT count(*) FROM catalog_publications")) == 1


async def test_catalog_backup_refuses_revision_mismatch(pg_db, tmp_path):
    from sqlalchemy import text
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.storage.backup import BackupService
    from tests.gateway.catalog_support import synthetic_vault

    vault = synthetic_vault()
    backup = tmp_path / "revision.enc"
    await BackupService(pg_db, vault).export(backup)
    async with empty_clone(pg_db) as clone:
        async with clone.sessions.begin() as session:
            await session.execute(text("UPDATE alembic_version SET version_num='0009_provider_admission'"))
        with pytest.raises(GatewayError, match="invalid_state"):
            await BackupService(clone, vault).restore(backup)


async def test_backup_roundtrip_preserves_cache_snapshots_and_pending_settlement(pg_db, tmp_path):
    from sqlalchemy import text
    from autobuild_json.gateway.storage.backup import BackupService
    from autobuild_json.gateway.metering.ledger import Ledger
    from autobuild_json.gateway.metering.records import Usage
    from tests.gateway.catalog_support import synthetic_vault
    from tests.gateway.harness import gateway_environment
    from .test_cache_ledger import admission_for, RATES, QUOTA
    from .test_ledger import bucket

    async with gateway_environment(pg_db, quota=QUOTA) as env:
        request = await admission_for(env, **RATES)
        await env.ledger.reserve(request)
        await env.ledger.mark_dispatched(request.request_id, uuid4())
        await env.ledger.mark_pending(request.request_id, "interrupted")
        vault = synthetic_vault()
        path = tmp_path / "cache.enc"
        await BackupService(pg_db, vault).export(path)
        async with empty_clone(pg_db) as clone:
            await BackupService(clone, vault).restore(path)
            async with clone.sessions() as session:
                row = (await session.execute(text("SELECT input_micro,output_micro,cache_read_micro,cache_write_micro FROM requests"))).one()
            assert row == (1_000_000, 3_000_000, 100_000, 1_250_000)
            assert await bucket(clone, env.key_id) == (0, 1_850_000_000)
            await Ledger(clone).settle(request.request_id, Usage(1000, 200, cached_read=600, cached_write=100))
            assert await bucket(clone, env.key_id) == (1_085_000_000, 0)
