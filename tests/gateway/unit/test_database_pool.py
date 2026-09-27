import asyncio

from pydantic import SecretStr


def test_database_uses_bounded_async_pool_and_accepts_limits():
    from autobuild_json.gateway.storage.db import make_database

    database = make_database(
        SecretStr("postgresql+psycopg://user:pass@127.0.0.1/service"),
        pool_size=3,
        max_overflow=2,
        pool_timeout=4,
    )
    try:
        pool = database.engine.pool
        assert pool.size() == 3
        assert pool._max_overflow == 2
        assert pool._timeout == 4
    finally:
        asyncio.run(database.close())


def test_database_rejects_capacity_that_can_deadlock_quota_refresh():
    from autobuild_json.gateway.storage.db import make_database

    try:
        make_database(
            SecretStr("postgresql+psycopg://user:pass@127.0.0.1/service"),
            pool_size=2,
            max_overflow=0,
        )
    except ValueError as error:
        assert str(error) == "Database pool capacity must be at least 3"
    else:
        raise AssertionError("quota refresh needs a lock connection plus worker sessions")
