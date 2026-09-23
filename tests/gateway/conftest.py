import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio


@pytest.fixture(scope="session")
def postgres_url():
    from scripts.gateway_testdb import test_database
    root = Path(__file__).resolve().parents[2]
    binaries = os.environ.get("AUTOBUILD_TEST_POSTGRES_BIN") or root / ".deps/gateway-pg17/binary/bin"
    with test_database(postgres_bin=binaries, url=os.environ.get("AUTOBUILD_TEST_DATABASE_URL")) as url:
        yield url


@pytest_asyncio.fixture
async def pg_db(postgres_url):
    from pydantic import SecretStr
    from sqlalchemy import text
    from autobuild_json.gateway.storage.db import make_database
    from autobuild_json.gateway.storage.migrate import upgrade
    from scripts.gateway_testdb import validate_test_url
    validate_test_url(postgres_url)
    schema = "abgw_test_" + uuid4().hex
    admin = make_database(SecretStr(postgres_url))
    database = make_database(SecretStr(postgres_url), schema=schema)
    async with admin.engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        await upgrade(database)
        yield database
    finally:
        await database.close()
        async with admin.engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.close()
