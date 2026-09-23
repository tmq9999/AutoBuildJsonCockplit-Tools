import asyncio
from datetime import datetime, timezone, timedelta
from uuid import uuid4

import pytest

from .test_oauth_import import credential_service, record

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_two_workers_refresh_once_and_commit_whole_token_chain(pg_db):
    calls = []
    async def exchange(tokens, lease, deadline):
        calls.append(tokens["refresh_token"])
        await asyncio.sleep(0.03)
        return {"id_token": "new-id", "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
    async def verify(tokens, account, email, lease, deadline):
        assert account == "account-test" and email == "synthetic@example.com"
    first = await credential_service(pg_db, exchange, verify)
    other = await credential_service(pg_db, exchange, verify)
    identity = await first.import_record(record(), None)
    end = datetime.now(timezone.utc)+timedelta(seconds=60)
    a, b = await asyncio.gather(first.fresh_tokens(identity, end), other.fresh_tokens(identity, end))
    assert a["access_token"] == b["access_token"] == "new-access"
    assert calls == ["synthetic-refresh"]


async def test_refresh_timeout_is_not_replayed(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    calls = []
    async def exchange(tokens, lease, deadline):
        calls.append(1)
        raise TimeoutError("unknown")
    service = await credential_service(pg_db, exchange, None)
    identity = await service.import_record(record(), None)
    for _ in range(2):
        with pytest.raises(GatewayError, match="refresh_uncertain"):
            await service.fresh_tokens(identity, datetime.now(timezone.utc)+timedelta(seconds=60))
    assert len(calls) == 1


async def test_refresh_generation_fences_out_stale_writer(pg_db):
    from sqlalchemy import text
    from autobuild_json.gateway.errors import GatewayError
    service = await credential_service(pg_db)
    identity = await service.import_record(record(), None)
    with pytest.raises(GatewayError, match="invalid_state"):
        await service.commit_refresh(identity, 0, uuid4(), {"id_token": "x", "access_token": "y", "refresh_token": "z"}, 3600)
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT token_generation FROM credentials WHERE id=:id"), {"id": identity}) == 0


async def test_verified_identity_mismatch_disables_account(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    async def exchange(tokens, lease, deadline):
        return {"id_token": "new-id", "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
    async def wrong_identity(*args):
        raise GatewayError("reauth_required", 401)
    service = await credential_service(pg_db, exchange, wrong_identity)
    identity = await service.import_record(record(), None)
    with pytest.raises(GatewayError, match="reauth_required"):
        await service.fresh_tokens(identity, datetime.now(timezone.utc)+timedelta(seconds=60))
