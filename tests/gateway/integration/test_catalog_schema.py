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
            assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "0011_catalog_schedule_completion"
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
