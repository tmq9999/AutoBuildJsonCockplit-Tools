import pytest
from uuid import uuid4

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_new_source_has_no_implicit_schedule_or_snapshot(pg_db):
    from sqlalchemy import text
    from tests.gateway.catalog_support import source_case

    sources, source = await source_case(pg_db)
    assert source.schedule_enabled is False and source.next_run_at is None
    assert source.latest_successful_run_id is None
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM catalog_entries")) == 0


async def test_catalog_schema_enforces_one_active_job_and_unique_source_pair(pg_db):
    import json
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from tests.gateway.catalog_support import source_case

    _, source = await source_case(pg_db)
    async with pg_db.sessions.begin() as session:
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(text("INSERT INTO catalog_sources(id,provider_id,credential_id,mode) "
                                           "VALUES (:id,:provider,:credential,'openai_single')"),
                                      {"id": uuid4(), "provider": source.provider_id,
                                       "credential": source.credential_id})
        await session.execute(text("INSERT INTO provider_operations"
                                   "(id,source_id,kind,state,payload_digest,input,expected,queued_expires_at) "
                                   "VALUES (:id,:source,'discover','queued',:digest,CAST(:input AS jsonb),"
                                   "CAST(:expected AS jsonb),clock_timestamp()+interval '10 minutes')"),
                              {"id": uuid4(), "source": source.id, "digest": b"a" * 32,
                               "input": json.dumps({}), "expected": json.dumps({})})
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(text("INSERT INTO provider_operations"
                                           "(id,source_id,kind,state,payload_digest,input,expected,queued_expires_at) "
                                           "VALUES (:id,:source,'check','running',:digest,CAST(:input AS jsonb),"
                                           "CAST(:expected AS jsonb),clock_timestamp()+interval '10 minutes')"),
                                      {"id": uuid4(), "source": source.id, "digest": b"b" * 32,
                                       "input": json.dumps({}), "expected": json.dumps({})})


async def test_catalog_upgrade_from_revision_nine_preserves_existing_rows(postgres_url):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    from pydantic import SecretStr
    from sqlalchemy import text
    from autobuild_json.gateway.storage.db import make_database

    schema = "abgw_test_" + uuid4().hex
    admin = make_database(SecretStr(postgres_url))
    db = make_database(SecretStr(postgres_url), schema=schema)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[3] /
                                                   "autobuild_json/gateway/storage/migrations"))
    try:
        async with admin.engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with db.engine.begin() as connection:
            await connection.run_sync(lambda sync: command.upgrade(_bind_config(config, sync), "0009_provider_admission"))
        provider_id = uuid4()
        async with db.sessions.begin() as session:
            await session.execute(text("INSERT INTO providers(id,config) VALUES (:id,'{}'::jsonb)"), {"id": provider_id})
        async with db.engine.begin() as connection:
            await connection.run_sync(lambda sync: command.upgrade(_bind_config(config, sync), "head"))
        async with db.sessions() as session:
            assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "0015_model_cooldown"
            assert await session.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_name='provider_operations' AND column_name='schedule_completed_at'")) == 1
            assert await session.scalar(text("SELECT count(*) FROM providers WHERE id=:id"),
                                        {"id": provider_id}) == 1
            assert await session.scalar(text("SELECT count(*) FROM catalog_sources")) == 0
            assert await session.scalar(text("SELECT count(*) FROM catalog_entries")) == 0
    finally:
        await db.close()
        async with admin.engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.close()


def _bind_config(config, connection):
    config.attributes["connection"] = connection
    return config


async def test_cache_rate_upgrade_preserves_historical_settlement_and_pending_hold(postgres_url):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    from pydantic import SecretStr
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from autobuild_json.gateway.storage.db import make_database
    from autobuild_json.gateway.metering.ledger import Ledger
    from autobuild_json.gateway.metering.records import Usage
    from .test_ledger import ledger_case, bucket

    schema = "abgw_test_" + uuid4().hex
    admin = make_database(SecretStr(postgres_url))
    db = make_database(SecretStr(postgres_url), schema=schema)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[3] /
                                                   "autobuild_json/gateway/storage/migrations"))
    pending, completed, entry = uuid4(), uuid4(), uuid4()
    try:
        async with admin.engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with db.engine.begin() as connection:
            await connection.run_sync(lambda sync: command.upgrade(_bind_config(config, sync), "0011_catalog_schedule_completion"))
        _, _, issued, _ = await ledger_case(db)
        async with db.sessions.begin() as session:
            for request_id, state in ((pending, "usage_pending"), (completed, "completed")):
                await session.execute(text("""INSERT INTO requests
                    (id,key_id,model_id,policy_version,protocol,state,admitted_at,deadline,periods,
                     input_micro,output_micro,input_bound,output_bound,hold,usage)
                    VALUES (:id,:key,'m',1,'openai',:state,now()-interval '25 hours',now()-interval '24 hours',
                     '{"total":"all","day":"2026-09-24","month":"2026-09"}',3,7,100,0,300,
                     CASE WHEN :state='completed' THEN '{"input_tokens": 10,"output_tokens": 0}'::jsonb ELSE NULL END)"""),
                    {"id": request_id, "key": issued.key_id, "state": state})
            await session.execute(text("INSERT INTO usage_ledger(id,request_id,entry_kind,amount_micro,source) "
                                       "VALUES (:id,:request,'settlement',30,'provider')"), {"id": entry, "request": completed})
            for window, period in (("total", "all"), ("day", "2026-09-24"), ("month", "2026-09")):
                await session.execute(text("INSERT INTO quota_buckets(key_id,window_kind,period,spent,held) "
                    "VALUES (:key,:window,:period,30,300)"), {"key": issued.key_id, "window": window, "period": period})
            original = (await session.execute(text("SELECT * FROM usage_ledger"))).mappings().one()
        async with db.engine.begin() as connection:
            await connection.run_sync(lambda sync: command.upgrade(_bind_config(config, sync), "head"))
        async with db.sessions.begin() as session:
            rows = (await session.execute(text("SELECT cache_read_micro,cache_write_micro FROM requests"))).all()
            assert rows == [(3, 3), (3, 3)]
            assert (await session.execute(text("SELECT * FROM usage_ledger"))).mappings().one() == original
            assert await session.scalar(text("SELECT usage FROM requests WHERE id=:id"), {"id": completed}) == {"input_tokens": 10, "output_tokens": 0}
            for column in ("cache_read_micro", "cache_write_micro"):
                for invalid in (None, -1):
                    with pytest.raises(IntegrityError):
                        async with session.begin_nested():
                            await session.execute(text(f"UPDATE requests SET {column}=:value WHERE id=:id"),
                                                  {"value": invalid, "id": pending})
        assert await bucket(db, issued.key_id) == (30, 300)
        await Ledger(db).settle(pending, Usage(10, 0, cached_read=6, cached_write=1))
        assert await bucket(db, issued.key_id) == (60, 0)
    finally:
        await db.close()
        async with admin.engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.close()
