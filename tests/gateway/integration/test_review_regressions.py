from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
import asyncio

import pytest
from sqlalchemy import text

from tests.gateway.harness import gateway_environment
from .test_gateway import BODY
from .test_oauth_import import credential_service, record
from .test_ledger import ledger_case, bucket

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_adapter_validation_failure_is_provably_not_dispatched(pg_db):
    from autobuild_json.gateway.routing.records import ProviderConfig

    async with gateway_environment(pg_db) as env:
        await env.engine.catalog.update_provider(
            env.provider_id,
            1,
            ProviderConfig(
                name="native",
                adapter="openai_compatible",
                wire_api="responses",
                root="https://provider.invalid/v1",
            ),
        )
        response = await env.client.post(
            "/v1/chat/completions",
            json=dict(BODY, stop=["END"]),
            headers={"Authorization": "Bearer " + env.secret},
        )
        assert response.status_code == 400 and not env.upstream_requests
        async with pg_db.sessions() as s:
            assert await s.scalar(text("SELECT count(*) FROM requests WHERE state='usage_pending'")) == 0


async def test_refresh_rechecks_uncertain_health_after_winning_lease(pg_db):
    from autobuild_json.gateway.errors import GatewayError

    calls = []

    async def exchange(*args):
        calls.append(1)
        raise TimeoutError()

    first = await credential_service(pg_db, exchange, None)
    second = await credential_service(pg_db, exchange, None)
    identity = await first.import_record(record(), None)
    original = second.refresh_leases.claim

    async def claim(*args):
        with pytest.raises(GatewayError, match="refresh_uncertain"):
            await first.fresh_tokens(identity, datetime.now(timezone.utc) + timedelta(seconds=60))
        return await original(*args)

    second.refresh_leases.claim = claim
    with pytest.raises(GatewayError, match="refresh_uncertain"):
        await second.fresh_tokens(identity, datetime.now(timezone.utc) + timedelta(seconds=60))
    assert len(calls) == 1


async def test_predispatch_budget_hold_is_recovered_with_request(pg_db):
    from autobuild_json.gateway.metering.budgets import BudgetService
    from autobuild_json.gateway.maintenance import Maintenance

    ledger, admission, issued, _ = await ledger_case(pg_db)
    request = admission(500)
    attempt = uuid4()
    await ledger.reserve(request)
    budgets = BudgetService(pg_db)
    budget = await budgets.create("USD", Decimal("1"))
    await budgets.reserve(budget, attempt, "USD", Decimal("1"), request_id=request.request_id)
    async with pg_db.sessions.begin() as s:
        await s.execute(
            text("UPDATE requests SET deadline=now()-interval '30 seconds' WHERE id=:id"),
            {"id": request.request_id},
        )
    await Maintenance(pg_db).tick()
    assert await bucket(pg_db, issued.key_id) == (0, 0)
    assert await budgets.balance(budget) == (Decimal(0), Decimal(0), Decimal(1))


async def test_recovery_resumes_money_refund_after_customer_already_released(pg_db):
    from autobuild_json.gateway.metering.budgets import BudgetService
    from autobuild_json.gateway.maintenance import Maintenance

    ledger, admission, _, _ = await ledger_case(pg_db)
    request = admission(100)
    await ledger.reserve(request)
    budgets = BudgetService(pg_db)
    budget = await budgets.create("USD", Decimal("1"))
    await budgets.reserve(budget, uuid4(), "USD", Decimal("1"), request_id=request.request_id)
    await ledger.release_unspent(request.request_id, "not_dispatched")
    async with pg_db.sessions.begin() as s:
        await s.execute(
            text("UPDATE requests SET deadline=now()-interval '30 seconds' WHERE id=:id"),
            {"id": request.request_id},
        )
    await Maintenance(pg_db).tick()
    assert await budgets.balance(budget) == (Decimal(0), Decimal(0), Decimal(1))


async def test_usage_bound_violation_quarantines_route_even_with_zero_coefficient(pg_db):
    import httpx
    import json

    rows = [
        {"choices": [{"index": 0, "delta": {"content": "Hi"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 2000, "completion_tokens": 5}},
    ]
    wire = b"".join(("data: " + json.dumps(r) + "\n\n").encode() for r in rows) + b"data: [DONE]\n\n"
    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(200, content=wire)) as env:
        from autobuild_json.gateway.identity.policy import KeyPolicy, ModelRate
        from autobuild_json.gateway.identity.service import IdentityService

        await IdentityService(pg_db, b"p" * 32).update_policy(
            env.key_id,
            1,
            KeyPolicy(
                model_ids={"public"},
                protocols={"openai"},
                total_micro=10**12,
                model_overrides=(ModelRate(model_id="public", input_micro=0, output_micro=0),),
            ),
        )
        headers = {"Authorization": "Bearer " + env.secret}
        first = await env.client.post("/v1/chat/completions", json=BODY, headers=headers)
        assert first.status_code == 200
        second = await env.client.post("/v1/chat/completions", json=BODY, headers=headers)
        assert second.status_code == 503 and len(env.upstream_requests) == 1


async def test_nonstream_disconnect_cancels_generation(pg_db):
    import json
    import httpx

    live = asyncio.Event()
    closed = asyncio.Event()

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            live.set()
            await asyncio.sleep(0.2)
            from tests.gateway.harness import chat_success

            yield chat_success()

        async def aclose(self):
            closed.set()

    async with gateway_environment(pg_db, respond=lambda r: httpx.Response(200, stream=Slow())) as env:
        body_sent = False

        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": json.dumps(BODY).encode(), "more_body": False}
            await live.wait()
            return {"type": "http.disconnect"}

        sent = []

        async def send(item):
            sent.append(item)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1:8788"),
                (b"content-type", b"application/json"),
                (b"authorization", ("Bearer " + env.secret).encode()),
            ],
            "client": ("127.0.0.1", 33333),
            "server": ("127.0.0.1", 8788),
        }
        await asyncio.wait_for(env.app(scope, receive, send), 2)
        assert closed.is_set()
        assert not any(x.get("status") == 200 for x in sent)
        async with pg_db.sessions() as s:
            assert await s.scalar(text("SELECT state FROM requests")) == "usage_pending"


async def test_configured_upstream_auth_mode_used_for_inference(pg_db):
    from autobuild_json.gateway.routing.records import ProviderConfig

    async with gateway_environment(pg_db) as env:
        await env.engine.catalog.update_provider(
            env.provider_id,
            1,
            ProviderConfig(
                name="custom",
                adapter="openai_compatible",
                root="https://provider.invalid/v1",
                auth_mode="x-api-key",
            ),
        )
        result = await env.client.post(
            "/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer " + env.secret}
        )
        assert result.status_code == 200
        assert env.upstream_requests[0]["headers"].get("x-api-key") == "synthetic-upstream-secret"
        assert "authorization" not in env.upstream_requests[0]["headers"]


async def test_provider_concurrency_is_shared_across_client_keys(pg_db):
    from autobuild_json.gateway.routing.records import ProviderConfig
    from autobuild_json.gateway.identity.service import IdentityService
    from autobuild_json.gateway.identity.policy import KeyPolicy
    from autobuild_json.gateway.protocols.openai_chat import OpenAIChatCodec
    from autobuild_json.gateway.errors import GatewayError

    async with gateway_environment(pg_db) as env:
        await env.engine.catalog.update_provider(
            env.provider_id,
            1,
            ProviderConfig(
                name="limited",
                adapter="openai_compatible",
                root="https://provider.invalid/v1",
                concurrency_limit=1,
                rpm_limit=60,
            ),
        )
        identity = IdentityService(pg_db, b"p" * 32)
        owner = await identity.create_customer("other")
        key = await identity.create_key(
            owner, KeyPolicy(model_ids={"public"}, protocols={"openai"}, total_micro=10**12)
        )
        one = await identity.authenticate(env.secret, "openai")
        two = await identity.authenticate(key.secret, "openai")
        request = OpenAIChatCodec().decode(BODY, {})
        held = await env.engine.prepare(one, request, env.engine.meta(BODY))
        try:
            with pytest.raises(GatewayError, match="upstream_unavailable"):
                await env.engine.prepare(two, request, env.engine.meta(BODY))
            assert len(env.upstream_requests) == 1
        finally:
            await held.close()


async def test_provider_rpm_remains_limited_after_completed_request(pg_db):
    from autobuild_json.gateway.routing.records import ProviderConfig

    async with gateway_environment(pg_db) as env:
        await env.engine.catalog.update_provider(
            env.provider_id,
            1,
            ProviderConfig(
                name="limited",
                adapter="openai_compatible",
                root="https://provider.invalid/v1",
                concurrency_limit=10,
                rpm_limit=1,
            ),
        )
        headers = {"Authorization": "Bearer " + env.secret}
        assert (await env.client.post("/v1/chat/completions", json=BODY, headers=headers)).status_code == 200
        assert (await env.client.post("/v1/chat/completions", json=BODY, headers=headers)).status_code == 503
        assert len(env.upstream_requests) == 1
