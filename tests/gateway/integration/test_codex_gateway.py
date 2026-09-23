from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

from tests.gateway.harness import gateway_environment
from .test_responses_gateway import native_wire
from .test_oauth_import import record, credential_service

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_codex_route_uses_account_tokens_and_preserves_unsupported_cap(pg_db):
    from autobuild_json.gateway.routing.records import ModelConfig, BindingConfig
    from autobuild_json.gateway.identity.policy import KeyPolicy
    from autobuild_json.gateway.identity.service import IdentityService
    from autobuild_json.gateway.accounts.imports import CODEX_PROVIDER
    async def exchange(tokens, lease, deadline):
        return {"id_token": "verified-id", "access_token": "verified-access", "refresh_token": "verified-refresh", "expires_in": 3600}
    async def verify(*args):
        return None
    service = await credential_service(pg_db, exchange, verify)
    credential = await service.import_record(record(), None)
    await service.fresh_tokens(credential, datetime.now(timezone.utc)+timedelta(seconds=60))
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(200, content=native_wire())) as env:
        env.engine.credentials = service
        await env.engine.catalog.put_model(ModelConfig(model_id="codex-test", identity="codex-test", enabled=True))
        await env.engine.catalog.put_binding(BindingConfig(id=uuid4(), provider_id=CODEX_PROVIDER, credential_id=credential,
            public_model_id="codex-test", upstream_model="codex-test", identity="codex-test", input_bound=1000, output_bound=500))
        await IdentityService(pg_db, b"p"*32).update_policy(env.key_id, 1,
            KeyPolicy(model_ids={"codex-test"}, protocols={"openai"}, total_micro=10**12))
        headers = {"Authorization": "Bearer "+env.secret}
        blocked = await env.client.post("/v1/responses", json={"model": "codex-test", "input": "hi", "max_output_tokens": 8}, headers=headers)
        assert blocked.status_code == 400 and not env.upstream_requests
        response = await env.client.post("/v1/responses", json={"model": "codex-test", "input": "hi"}, headers=headers)
        assert response.status_code == 200, response.text
        outbound = env.upstream_requests[0]
        assert outbound["headers"]["authorization"] == "Bearer verified-access"
        assert outbound["headers"]["chatgpt-account-id"] == "account-test"
        assert "max_output_tokens" not in outbound["body"]
