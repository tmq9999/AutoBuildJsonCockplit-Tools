from datetime import datetime, timezone, timedelta
import json

import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def setup_identity(pg_db, **policy_values):
    from autobuild_json.gateway.identity.service import IdentityService
    from autobuild_json.gateway.identity.policy import KeyPolicy
    service = IdentityService(pg_db, pepper=b"p" * 32)
    owner = await service.create_customer("Customer test")
    issued = await service.create_key(owner, KeyPolicy(protocols={"openai"}, **policy_values))
    return service, owner, issued


async def test_rotation_keeps_owner_and_revokes_old_secret(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    service, owner, issued = await setup_identity(pg_db)
    rotated = await service.rotate(issued.key_id, issued.version)
    principal = await service.authenticate(rotated.secret, "openai")
    assert principal.key_id == issued.key_id and principal.customer_id == owner
    with pytest.raises(GatewayError, match="invalid_api_key"):
        await service.authenticate(issued.secret, "openai")
    assert issued.secret not in repr(issued)


async def test_disable_customer_blocks_all_keys(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    service, owner, issued = await setup_identity(pg_db)
    await service.set_customer_enabled(owner, False, version=1)
    with pytest.raises(GatewayError, match="invalid_api_key"):
        await service.authenticate(issued.secret, "openai")


async def test_expired_key_and_wrong_protocol_are_rejected(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    service, _, issued = await setup_identity(pg_db, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    with pytest.raises(GatewayError, match="invalid_api_key"):
        await service.authenticate(issued.secret, "openai")
    service, _, live = await setup_identity(pg_db)
    with pytest.raises(GatewayError, match="permission_denied"):
        await service.authenticate(live.secret, "anthropic")


async def test_policy_update_uses_version_and_preserves_key_id(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.identity.policy import KeyPolicy
    service, _, issued = await setup_identity(pg_db)
    updated = await service.update_policy(issued.key_id, issued.version,
        KeyPolicy(model_ids={"m"}, protocols={"openai"}, total_micro=100))
    assert updated.version == issued.version + 1
    assert updated.policy.total_micro == 100
    with pytest.raises(GatewayError, match="version_conflict"):
        await service.update_policy(issued.key_id, issued.version, KeyPolicy())
    assert (await service.authenticate(issued.secret, "openai")).key_id == issued.key_id


async def test_revoke_is_idempotent_and_keeps_audit(pg_db):
    from sqlalchemy import text
    from autobuild_json.gateway.errors import GatewayError
    service, _, issued = await setup_identity(pg_db)
    await service.revoke(issued.key_id, issued.version)
    await service.revoke(issued.key_id, issued.version)
    with pytest.raises(GatewayError, match="invalid_api_key"):
        await service.authenticate(issued.secret, "openai")
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM api_keys")) == 1
        audit = (await session.execute(text("SELECT action, details FROM audit_events"))).all()
    assert [row[0] for row in audit].count("key.revoked") == 1
    assert issued.secret not in str(audit)


async def test_list_keys_and_database_do_not_contain_plaintext(pg_db):
    from sqlalchemy import text
    service, owner, issued = await setup_identity(pg_db)
    views = await service.list_keys(owner)
    payload = json.dumps([view.model_dump(mode="json") for view in views])
    assert issued.secret not in payload
    assert "secret_digest" not in payload
    assert "secret" not in payload
    async with pg_db.sessions() as session:
        rows = (await session.execute(text("SELECT row_to_json(k)::text FROM api_keys k"))).scalars().all()
    assert issued.secret not in "".join(rows)


async def test_concurrent_rotations_allow_only_one_writer(pg_db):
    import asyncio
    from autobuild_json.gateway.errors import GatewayError
    service, _, issued = await setup_identity(pg_db)
    results = await asyncio.gather(service.rotate(issued.key_id, issued.version),
                                   service.rotate(issued.key_id, issued.version), return_exceptions=True)
    assert sum(not isinstance(row, Exception) for row in results) == 1
    assert sum(isinstance(row, GatewayError) and row.code == "version_conflict" for row in results) == 1


async def test_disabled_key_policy_blocks_authentication(pg_db):
    from autobuild_json.gateway.identity.policy import KeyPolicy
    from autobuild_json.gateway.errors import GatewayError
    service, _, issued = await setup_identity(pg_db)
    await service.update_policy(issued.key_id, issued.version, KeyPolicy(enabled=False, protocols={"openai"}))
    with pytest.raises(GatewayError, match="invalid_api_key"):
        await service.authenticate(issued.secret, "openai")
