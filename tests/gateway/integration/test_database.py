from uuid import uuid4

import pytest


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_identity_migration_enforces_unique_digest(pg_db):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    customer = uuid4()
    async with pg_db.sessions.begin() as session:
        await session.execute(text("INSERT INTO customers (id, name) VALUES (:id, 'Test')"), {"id": customer})
        await session.execute(text("INSERT INTO api_keys (id, customer_id, secret_digest, prefix) "
                                   "VALUES (:id, :customer, :digest, 'test')"),
                              {"id": uuid4(), "customer": customer, "digest": bytes(32)})
    with pytest.raises(IntegrityError):
        async with pg_db.sessions.begin() as session:
            await session.execute(text("INSERT INTO api_keys (id, customer_id, secret_digest, prefix) "
                                       "VALUES (:id, :customer, :digest, 'duplicate')"),
                                  {"id": uuid4(), "customer": customer, "digest": bytes(32)})


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_database_commits_are_visible_across_sessions(pg_db):
    from sqlalchemy import text
    customer = uuid4()
    async with pg_db.sessions.begin() as writer:
        await writer.execute(text("INSERT INTO customers (id, name) VALUES (:id, 'visible')"), {"id": customer})
    async with pg_db.sessions() as reader:
        assert await reader.scalar(text("SELECT name FROM customers WHERE id=:id"), {"id": customer}) == "visible"


def test_test_database_helper_rejects_non_test_target():
    from scripts.gateway_testdb import validate_test_url
    with pytest.raises(ValueError, match="dedicated"):
        validate_test_url("postgresql+psycopg://user:secret@localhost/production")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_migrations_can_be_repeated_without_erasing_identity(pg_db):
    from sqlalchemy import text
    from autobuild_json.gateway.storage.migrate import upgrade
    customer = uuid4()
    async with pg_db.sessions.begin() as writer:
        await writer.execute(text("INSERT INTO customers (id, name) VALUES (:id, 'retained')"), {"id": customer})
    await upgrade(pg_db)
    async with pg_db.sessions() as reader:
        assert await reader.scalar(text("SELECT name FROM customers WHERE id=:id"), {"id": customer}) == "retained"
        assert await reader.scalar(text("SELECT count(*) FROM audit_events")) == 0


def test_unsafe_schema_is_rejected_before_connecting():
    from pydantic import SecretStr
    from autobuild_json.gateway.storage.db import make_database
    with pytest.raises(ValueError, match="Invalid test schema"):
        make_database(SecretStr("postgresql+psycopg://u:p@localhost/test"), schema="public; DROP SCHEMA public")
