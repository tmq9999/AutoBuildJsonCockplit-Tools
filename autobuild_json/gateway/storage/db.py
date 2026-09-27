import re
from dataclasses import dataclass

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


@dataclass(repr=False)
class Database:
    engine: AsyncEngine
    sessions: async_sessionmaker

    async def close(self):
        await self.engine.dispose()


def make_database(url: SecretStr, *, schema: str | None = None, pool_size: int = 16,
                  max_overflow: int = 8, pool_timeout: float = 30) -> Database:
    value = url.get_secret_value()
    if not value.startswith("postgresql+psycopg://"):
        raise ValueError("PostgreSQL psycopg URL required")
    options = {}
    if schema is not None:
        if not re.fullmatch(r"abgw_test_[a-f0-9]{32}", schema):
            raise ValueError("Invalid test schema")
        options["options"] = f"-csearch_path={schema}"
    if pool_size + max_overflow < 3:
        raise ValueError("Database pool capacity must be at least 3")
    # A bounded pool prevents each concurrent request from opening a new
    # PostgreSQL session.  The gateway runs the public and admin apps in
    # separate processes, so an explicit per-process ceiling is important.
    engine = create_async_engine(value, echo=False, hide_parameters=True,
                                 pool_size=pool_size, max_overflow=max_overflow,
                                 pool_timeout=pool_timeout, connect_args=options)
    return Database(engine, async_sessionmaker(engine, expire_on_commit=False))
