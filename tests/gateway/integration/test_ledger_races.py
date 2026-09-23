import asyncio
from concurrent.futures import ProcessPoolExecutor
import multiprocessing

import pytest

from .test_ledger import ledger_case, bucket


def reserve_in_process(url, schema, admission):
    from pydantic import SecretStr
    from autobuild_json.gateway.storage.db import make_database
    from autobuild_json.gateway.metering.ledger import Ledger
    from autobuild_json.gateway.errors import GatewayError
    async def reserve():
        db = make_database(SecretStr(url), schema=schema)
        try:
            await Ledger(db).reserve(admission)
            return "ok"
        except GatewayError as exc:
            return exc.code
        finally:
            await db.close()
    return asyncio.run(reserve())


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_processes_share_one_quota_authority(pg_db):
    from sqlalchemy import text
    _, admission, issued, _ = await ledger_case(pg_db)
    async with pg_db.sessions() as session:
        schema = await session.scalar(text("SELECT current_schema()"))
    url = pg_db.engine.url.render_as_string(hide_password=False)
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        loop = asyncio.get_running_loop()
        results = await asyncio.gather(
            loop.run_in_executor(pool, reserve_in_process, url, schema, admission(700)),
            loop.run_in_executor(pool, reserve_in_process, url, schema, admission(700)))
    assert sorted(results) == ["ok", "quota_exceeded"]
    assert await bucket(pg_db, issued.key_id) == (0, 700)
