import re
from dataclasses import dataclass

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


@dataclass(repr=False)
class Database:
    engine: AsyncEngine
    sessions: async_sessionmaker

    async def close(self):
        await self.engine.dispose()


def make_database(url: SecretStr, *, schema: str | None = None) -> Database:
    value = url.get_secret_value()
    if not value.startswith("postgresql+psycopg://"):
        raise ValueError("PostgreSQL psycopg URL required")
    options = {}
    if schema is not None:
        if not re.fullmatch(r"abgw_test_[a-f0-9]{32}", schema):
            raise ValueError("Invalid test schema")
        options["options"] = f"-csearch_path={schema}"
    engine = create_async_engine(value, echo=False, hide_parameters=True, poolclass=NullPool,
                                 connect_args=options)
    return Database(engine, async_sessionmaker(engine, expire_on_commit=False))
