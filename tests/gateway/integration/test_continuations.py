from datetime import datetime, timezone, timedelta
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_continuation_is_encrypted_and_scoped_to_customer_key_model(pg_db):
    from sqlalchemy import text
    from autobuild_json.gateway.routing.continuations import ContinuationScope, ContinuationStore, ContinuationBinding
    from autobuild_json.gateway.secrets import Vault
    from autobuild_json.gateway.errors import GatewayError
    store = ContinuationStore(pg_db, Vault({"v1": b"a"*32}, active="v1"))
    scope = ContinuationScope(uuid4(), uuid4(), "model")
    binding = ContinuationBinding(uuid4(), uuid4(), "upstream-private-id", datetime.now(timezone.utc)+timedelta(seconds=60))
    handle = await store.issue(scope, binding)
    assert (await store.resolve(handle, scope)).upstream_id == "upstream-private-id"
    for wrong in [ContinuationScope(scope.customer_id, uuid4(), "model"),
                  ContinuationScope(uuid4(), scope.key_id, "model"), ContinuationScope(scope.customer_id, scope.key_id, "other")]:
        with pytest.raises(GatewayError, match="invalid_state"):
            await store.resolve(handle, wrong)
    async with pg_db.sessions() as session:
        stored = await session.scalar(text("SELECT encrypted_upstream_id::text FROM continuation_handles"))
    assert "upstream-private-id" not in stored


async def test_expired_handle_never_falls_back(pg_db):
    from autobuild_json.gateway.routing.continuations import ContinuationScope, ContinuationStore, ContinuationBinding
    from autobuild_json.gateway.secrets import Vault
    from autobuild_json.gateway.errors import GatewayError
    store = ContinuationStore(pg_db, Vault({"v1": b"a"*32}, active="v1"))
    scope = ContinuationScope(uuid4(), uuid4(), "model")
    binding = ContinuationBinding(uuid4(), uuid4(), "private", datetime.now(timezone.utc)-timedelta(seconds=1))
    with pytest.raises(GatewayError, match="invalid_state"):
        await store.issue(scope, binding)
