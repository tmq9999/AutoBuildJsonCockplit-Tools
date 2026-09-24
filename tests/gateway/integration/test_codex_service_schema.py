import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_codex_tables_exist(pg_db):
    names = ("codex_account_state", "codex_reset_requests", "account_pool_policies",
             "account_session_bindings", "key_quota_adjustments")
    async with pg_db.sessions() as session:
        for name in names:
            assert await session.scalar(text("SELECT to_regclass(:name)"), {"name": name}) == name


async def test_codex_dtos_are_strict_and_fenced():
    from datetime import datetime, timezone
    from decimal import Decimal
    from autobuild_json.gateway.accounts.quota_records import QuotaWindow, CodexQuotaSnapshot
    from uuid import uuid4

    now = datetime.now(timezone.utc)
    row = QuotaWindow(used_percent=Decimal(42), window_seconds=1800, reset_at=now)
    snap = CodexQuotaSnapshot(credential_id=uuid4(), version=1, fetched_at=now, primary=row)
    assert snap.primary.used_percent == 42
    with pytest.raises(ValueError):
        QuotaWindow(used_percent=Decimal(101), window_seconds=1, reset_at=now)
    with pytest.raises(ValueError):
        QuotaWindow(used_percent=Decimal(1), window_seconds=1, reset_at=now, extra="nope")


async def test_records_preserve_unknowns_and_reject_unsafe_or_mutable_values():
    from datetime import datetime
    from decimal import Decimal
    from uuid import uuid4
    from autobuild_json.gateway.accounts.quota_records import QuotaWindow, ResetCredit
    from autobuild_json.gateway.routing.pool_records import AccountPoolPolicy, PoolMember

    assert QuotaWindow(used_percent=None, window_seconds=0).used_percent is None
    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-1")):
        with pytest.raises(ValueError):
            QuotaWindow(used_percent=bad)
    for bad in (True, "10", -1):
        with pytest.raises(ValueError):
            QuotaWindow(window_seconds=bad)
    with pytest.raises(ValueError):
        QuotaWindow(reset_at=datetime.now())
    with pytest.raises(ValueError):
        ResetCredit(id="abc\nsecret", state="available")
    member = PoolMember(credential_id=uuid4())
    with pytest.raises(ValueError):
        AccountPoolPolicy(members=(member, member))
    with pytest.raises(ValueError):
        AccountPoolPolicy(mode="single", members=(member,))
    with pytest.raises(ValueError):
        AccountPoolPolicy(plan_order=("free", "free"))
    with pytest.raises(ValueError):
        AccountPoolPolicy(plan_order=("\x00paid",))
    with pytest.raises(ValueError):
        member.priority = 2


async def test_account_schema_constraints(pg_db):
    from uuid import uuid4
    from sqlalchemy.exc import IntegrityError
    from .test_codex_quota_store import account

    credential = await account(pg_db)
    async with pg_db.sessions.begin() as session:
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(text("INSERT INTO codex_account_state(credential_id) VALUES (:id)"), {"id": uuid4()})
        await session.execute(text("INSERT INTO codex_account_state(credential_id) VALUES (:id)"), {"id": credential})
        for column, value in (("version", 0), ("generation", -1), ("failure_count", -1)):
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await session.execute(text(f"UPDATE codex_account_state SET {column}=:value"), {"value": value})
        for state, expiry in (("invalid", 30), ("prepared", -1)):
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await session.execute(text("INSERT INTO codex_reset_requests(id,credential_id,redeem_request_id,payload_digest,"
                        "state,actor,deadline,stamp,credits_version) VALUES (:id,:credential,:redeem,:digest,:state,'admin',"
                        "clock_timestamp()+make_interval(secs=>:expiry),'{}',1)"),
                        {"id": uuid4(), "credential": credential, "redeem": uuid4(), "digest": b"a" * 32,
                         "state": state, "expiry": expiry})


async def test_reset_unknown_backup_preserves_lock_but_fences_live_claims(pg_db, tmp_path):
    from autobuild_json.gateway.storage.backup import BackupService
    from autobuild_json.gateway.accounts.quota_store import QuotaStore
    from autobuild_json.gateway.errors import GatewayError
    from tests.gateway.catalog_support import synthetic_vault
    from .test_codex_quota_store import seeded, deadline
    from .test_backups import empty_clone
    from uuid import uuid4

    store, credential, credits = await seeded(pg_db)
    claim = await store.prepare_reset(credential, uuid4(), credits.version, "admin", deadline=deadline())
    assert await store.mark_dispatched(claim.result.operation_id, claim.generation)
    await store.finish_reset(claim.result.operation_id, claim.generation, "unknown", "codex_reset_uncertain", None)
    refresh = await store.begin_refresh(credential, deadline())
    path = tmp_path / "codex.enc"
    vault = synthetic_vault()
    await BackupService(pg_db, vault).export(path)
    async with empty_clone(pg_db) as clone:
        await BackupService(clone, vault).restore(path)
        restored = QuotaStore(clone)
        assert not await restored.save_refresh(refresh, None, None, ())
        with pytest.raises(GatewayError, match="operation_conflict"):
            await restored.prepare_reset(credential, uuid4(), credits.version, "admin", deadline=deadline())


@pytest.mark.parametrize("state,restored_state", [("prepared", "rejected"), ("dispatched", "unknown"),
    ("succeeded_refresh_failed", "succeeded_refresh_failed"), ("rejected", "rejected"), ("succeeded", "succeeded")])
async def test_backup_invalidates_only_runtime_reset_claims(pg_db, tmp_path, state, restored_state):
    from uuid import uuid4
    from autobuild_json.gateway.storage.backup import BackupService
    from autobuild_json.gateway.accounts.quota_store import QuotaStore
    from tests.gateway.catalog_support import synthetic_vault
    from .test_backups import empty_clone
    from .test_codex_quota_store import seeded, deadline

    store, credential, credits = await seeded(pg_db)
    claim = await store.prepare_reset(credential, uuid4(), credits.version, "admin", deadline=deadline())
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE codex_reset_requests SET state=:state"), {"state": state})
        before = (await session.execute(text("SELECT * FROM codex_reset_requests"))).mappings().one()
    path = tmp_path / "codex-reset.enc"
    vault = synthetic_vault()
    await BackupService(pg_db, vault).export(path)
    async with empty_clone(pg_db) as clone:
        await BackupService(clone, vault).restore(path)
        async with clone.sessions() as session:
            after = (await session.execute(text("SELECT * FROM codex_reset_requests"))).mappings().one()
        assert after["state"] == restored_state
        assert after["redeem_request_id"] == before["redeem_request_id"]
        if state in {"prepared", "dispatched"}:
            assert after["generation"] > before["generation"]
        elif state == "succeeded_refresh_failed":
            assert after["generation"] > before["generation"]
            assert {key: value for key, value in after.items() if key not in {"generation", "version"}} == {
                key: value for key, value in before.items() if key not in {"generation", "version"}}
        else:
            assert after == before
        assert not await QuotaStore(clone).mark_dispatched(claim.result.operation_id, claim.generation)


async def test_upgrade_from_twelve_preserves_attempt_history(postgres_url):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    from uuid import uuid4
    from pydantic import SecretStr
    from autobuild_json.gateway.storage.db import make_database
    from .test_catalog_schema import _bind_config
    from .test_ledger import ledger_case

    schema = "abgw_test_" + uuid4().hex
    admin = make_database(SecretStr(postgres_url))
    db = make_database(SecretStr(postgres_url), schema=schema)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[3] / "autobuild_json/gateway/storage/migrations"))
    try:
        async with admin.engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with db.engine.begin() as connection:
            await connection.run_sync(lambda sync: command.upgrade(_bind_config(config, sync), "0012_cache_rates"))
        ledger, admission, _, _ = await ledger_case(db)
        request = admission(10)
        await ledger.reserve(request)
        await ledger.mark_dispatched(request.request_id, uuid4())
        async with db.sessions() as session:
            before = dict((await session.execute(text("SELECT * FROM attempts"))).mappings().one())
        async with db.engine.begin() as connection:
            await connection.run_sync(lambda sync: command.upgrade(_bind_config(config, sync), "head"))
        async with db.sessions() as session:
            after = dict((await session.execute(text("SELECT * FROM attempts"))).mappings().one())
        assert after == {**before, "provider_id": None, "credential_id": None, "binding_id": None, "config_version": None}
    finally:
        await db.close()
        async with admin.engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.close()


async def test_quota_adjustment_ledger_is_append_only_and_nonnegative(pg_db):
    from uuid import uuid4
    from sqlalchemy.exc import DBAPIError, IntegrityError
    from .test_ledger import ledger_case

    _, _, key, _ = await ledger_case(pg_db)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("INSERT INTO key_quota_adjustments(id,key_id,version_before,version_after,amount_micro,actor,reason) "
            "VALUES (:id,:key,1,2,5,'admin','verified adjustment')"), {"id": uuid4(), "key": key.key_id})
        for command in ("DELETE FROM key_quota_adjustments", "UPDATE key_quota_adjustments SET amount_micro=6"):
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(text(command))
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(text("INSERT INTO key_quota_adjustments(id,key_id,version_before,version_after,amount_micro,actor,reason) "
                    "VALUES (:id,:key,2,3,-1,'admin','verified adjustment')"), {"id": uuid4(), "key": key.key_id})


async def test_pool_session_and_attempt_foreign_keys_and_limits(pg_db):
    from sqlalchemy.exc import IntegrityError
    from uuid import uuid4
    from .test_catalog import catalog_case
    from .test_ledger import ledger_case

    _, principal, provider, credential, binding = await catalog_case(pg_db)
    ledger, admission, key, _ = await ledger_case(pg_db)
    request = admission(1)
    await ledger.reserve(request)
    await ledger.mark_dispatched(request.request_id, uuid4())
    async with pg_db.sessions.begin() as session:
        await session.execute(text("INSERT INTO account_pool_policies(model_id,policy) VALUES ('public','{}')"))
        await session.execute(text("INSERT INTO account_session_bindings(scope_digest,customer_id,key_id,model_id,binding_id,credential_id,expires_at) "
            "VALUES (:digest,:customer,:key,'public',:binding,:credential,clock_timestamp()+interval '10 minutes')"),
            {"digest": b"d" * 32, "customer": principal.customer_id, "key": key.key_id, "binding": binding.id, "credential": credential})
        for query, params in (
            ("UPDATE account_pool_policies SET version=0", {}),
            ("UPDATE account_pool_policies SET model_id='missing'", {}),
            ("UPDATE account_session_bindings SET scope_digest=:digest", {"digest": b"short"}),
            ("UPDATE account_session_bindings SET credential_id=:id", {"id": uuid4()}),
            ("UPDATE account_session_bindings SET key_id=:id", {"id": uuid4()}),
            ("UPDATE account_session_bindings SET customer_id=:id", {"id": uuid4()}),
            ("UPDATE account_session_bindings SET binding_id=:id", {"id": uuid4()}),
            ("UPDATE account_session_bindings SET version=0", {}),
            ("UPDATE attempts SET credential_id=:id", {"id": uuid4()}),
            ("UPDATE attempts SET provider_id=:id", {"id": uuid4()}),
            ("UPDATE attempts SET binding_id=:id", {"id": uuid4()}),
            ("UPDATE attempts SET config_version=0", {}),
        ):
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await session.execute(text(query), params)
        await session.execute(text("UPDATE attempts SET provider_id=:provider,credential_id=:credential,binding_id=:binding,config_version=1"),
                              {"provider": provider, "credential": credential, "binding": binding.id})


async def test_new_tables_all_round_trip_with_attempt_attribution(pg_db, tmp_path):
    from autobuild_json.gateway.storage.backup import BackupService
    from tests.gateway.catalog_support import synthetic_vault
    from .test_backups import empty_clone

    await test_pool_session_and_attempt_foreign_keys_and_limits(pg_db)
    await test_quota_adjustment_ledger_is_append_only_and_nonnegative(pg_db)
    vault = synthetic_vault()
    path = tmp_path / "all-codex.enc"
    async with pg_db.sessions() as session:
        before = {name: [dict(row) for row in (await session.execute(text(f"SELECT * FROM {name}"))).mappings()]
                  for name in ("account_pool_policies", "account_session_bindings", "key_quota_adjustments", "attempts")}
    await BackupService(pg_db, vault).export(path)
    async with empty_clone(pg_db) as clone:
        await BackupService(clone, vault).restore(path)
        async with clone.sessions() as session:
            after = {name: [dict(row) for row in (await session.execute(text(f"SELECT * FROM {name}"))).mappings()]
                     for name in before}
        assert after == before


async def test_restore_fences_partial_success_worker_without_rewriting_receipt(pg_db, tmp_path):
    from uuid import uuid4
    from autobuild_json.gateway.storage.backup import BackupService
    from autobuild_json.gateway.accounts.quota_store import QuotaStore
    from autobuild_json.gateway.errors import GatewayError
    from tests.gateway.catalog_support import synthetic_vault
    from .test_backups import empty_clone
    from .test_codex_quota_store import seeded, deadline, snapshots

    store, credential, credits = await seeded(pg_db)
    claim = await store.prepare_reset(credential, uuid4(), credits.version, "admin", deadline=deadline())
    assert await store.mark_dispatched(claim.result.operation_id, claim.generation)
    await store.finish_reset(claim.result.operation_id, claim.generation, "succeeded_refresh_failed", "codex_refresh_failed", 200)
    refresh = await store.begin_refresh(credential, deadline())
    assert await store.save_refresh(refresh, *snapshots(credential), ())
    async with pg_db.sessions() as session:
        before = dict((await session.execute(text("SELECT * FROM codex_reset_requests"))).mappings().one())
    vault = synthetic_vault()
    path = tmp_path / "partial.enc"
    await BackupService(pg_db, vault).export(path)
    async with empty_clone(pg_db) as clone:
        await BackupService(clone, vault).restore(path)
        with pytest.raises(GatewayError, match="claim_lost"):
            await QuotaStore(clone).finish_reset(claim.result.operation_id, claim.generation, "succeeded", None, 200)
        async with clone.sessions() as session:
            after = dict((await session.execute(text("SELECT * FROM codex_reset_requests"))).mappings().one())
        assert after.pop("generation") > before.pop("generation")
        assert after.pop("version") > before.pop("version")
        assert after == before
