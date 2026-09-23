from uuid import uuid4
import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def record():
    return {"id": str(uuid4()), "email": "synthetic@example.com", "account": {"id": "account-test"},
            "tokens": {"id_token": "synthetic-id", "access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}}


async def credential_service(db, exchange=None, verifier=None):
    from autobuild_json.gateway.accounts.imports import CredentialService
    from autobuild_json.gateway.secrets import Vault
    from autobuild_json.gateway.proxy.pg_leases import PgLeaseStore
    from autobuild_json.gateway.proxy.manager import ProxyManager
    return CredentialService(db, Vault({"v1": b"a"*32}, active="v1"), ProxyManager(PgLeaseStore(db)),
                             transport=None, exchange=exchange, verifier=verifier)


async def test_import_is_encrypted_unverified_and_deduplicated(pg_db):
    from sqlalchemy import text
    service = await credential_service(pg_db)
    source = record()
    first = await service.import_record(source, None)
    second = await service.import_record(source, None)
    assert first == second
    async with pg_db.sessions() as session:
        row = (await session.execute(text("SELECT health,encrypted_secret::text FROM credentials"))).one()
    assert row[0] == "unverified"
    assert "synthetic-refresh" not in row[1]
    assert source["tokens"]["refresh_token"] == "synthetic-refresh"


async def test_missing_refresh_token_is_rejected(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    service = await credential_service(pg_db)
    source = record()
    del source["tokens"]["refresh_token"]
    with pytest.raises(GatewayError, match="invalid_request"):
        await service.import_record(source, None)
