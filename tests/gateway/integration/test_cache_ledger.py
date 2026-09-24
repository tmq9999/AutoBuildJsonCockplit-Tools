"""Customer cache rates are frozen, exact, and independent of provider costs."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from autobuild_json.gateway.identity.policy import KeyPolicy, ModelRate
from autobuild_json.gateway.identity.service import IdentityService
from autobuild_json.gateway.metering.ledger import Ledger
from autobuild_json.gateway.metering.records import Admission, Bounds, Usage
from autobuild_json.gateway.protocols.openai_chat import OpenAIChatCodec
from autobuild_json.gateway.routing.records import ModelConfig
from tests.gateway.harness import chat_success, gateway_environment
from .test_ledger import bucket

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
QUOTA = 100_000_000_000_000
RATES = dict(input_micro=1_000_000, output_micro=3_000_000,
             cache_read_micro=100_000, cache_write_micro=1_250_000)
BODY = {"model": "public", "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 200}


async def admission_for(env, **rates):
    identity = IdentityService(env.engine.db, b"p" * 32)
    principal = await identity.authenticate(env.secret, "openai")
    return Admission(uuid4(), principal, "public", Bounds(1000, 200),
                     deadline=datetime.now(timezone.utc) + timedelta(seconds=60), **rates)


async def test_cache_override_settles_once_with_exact_remaining_quota(pg_db):
    async with gateway_environment(pg_db, quota=QUOTA) as env:
        identity = IdentityService(pg_db, b"p" * 32)
        await identity.update_policy(env.key_id, 1, KeyPolicy(
            model_ids={"public"}, protocols={"openai"}, total_micro=QUOTA,
            model_overrides=(ModelRate(model_id="public", **RATES),)))
        request = await admission_for(env, input_micro=9, output_micro=9)
        await env.ledger.reserve(request)
        await env.ledger.mark_dispatched(request.request_id, uuid4())
        usage = Usage(1000, 200, cached_read=600, cached_write=100)
        await asyncio.gather(*(env.ledger.settle(request.request_id, usage) for _ in range(8)))
        spent, held = await bucket(pg_db, env.key_id)
        assert (spent, held, QUOTA - spent) == (1_085_000_000, 0, 99_998_915_000_000)
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 1
            saved = await session.scalar(text("SELECT usage FROM requests"))
        assert saved == dict(input_tokens=1000, output_tokens=200, cached_read=600,
                             cached_write=100, reasoning=0, source="provider",
                             computed_micro=1_085_000_000, charged_micro=1_085_000_000)


@pytest.mark.parametrize("override,expected", [(None, 1_600_000_000),
    (dict(input_micro=2_000_000, output_micro=3_000_000), 2_600_000_000),
    (dict(input_micro=1_000_000, output_micro=3_000_000,
          cache_read_micro=0, cache_write_micro=0), 900_000_000)])
async def test_null_cache_rates_inherit_effective_input_and_zero_is_free(pg_db, override, expected):
    async with gateway_environment(pg_db, quota=QUOTA) as env:
        rates = dict(input_micro=1_000_000, output_micro=3_000_000)
        if override:
            rates.update(cache_read_micro=7_000_000, cache_write_micro=8_000_000)
            await IdentityService(pg_db, b"p" * 32).update_policy(env.key_id, 1, KeyPolicy(
                model_ids={"public"}, protocols={"openai"}, total_micro=QUOTA,
                model_overrides=(ModelRate(model_id="public", **override),)))
        request = await admission_for(env, **rates)
        await env.ledger.reserve(request)
        await env.ledger.mark_dispatched(request.request_id, uuid4())
        await env.ledger.settle(request.request_id, Usage(1000, 200, cached_read=600, cached_write=100))
        assert await bucket(pg_db, env.key_id) == (expected, 0)
        async with pg_db.sessions() as session:
            row = (await session.execute(text("SELECT * FROM requests"))).mappings().one()
        effective = 0 if override and override.get("cache_read_micro") == 0 else (override or rates)["input_micro"]
        assert (row["cache_read_micro"], row["cache_write_micro"]) == (effective, effective)


async def test_cache_write_maximum_and_resize_use_frozen_rates(pg_db):
    async with gateway_environment(pg_db, quota=QUOTA) as env:
        request = await admission_for(env, **RATES)
        hold = await env.ledger.reserve(request)
        assert hold.amount_micro == 1_850_000_000
        await IdentityService(pg_db, b"p" * 32).update_policy(env.key_id, 1, KeyPolicy(
            model_ids={"public"}, protocols={"openai"}, total_micro=QUOTA,
            model_overrides=(ModelRate(model_id="public", input_micro=9, output_micro=9,
                                       cache_read_micro=0, cache_write_micro=0),)))
        resized = await env.ledger.resize(hold, Bounds(2000, 200))
        assert resized.amount_micro == 3_100_000_000
        await env.ledger.mark_dispatched(request.request_id, uuid4())
        await env.ledger.settle(request.request_id, Usage(1000, 200, cached_read=600, cached_write=100))
        assert await bucket(pg_db, env.key_id) == (1_085_000_000, 0)


@pytest.mark.parametrize("override", [False, True])
async def test_engine_freezes_model_and_key_rates_during_stream(pg_db, override):
    wire = b'data: {"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 600}}}\n\ndata: [DONE]\n\n'
    wire = chat_success(False).removesuffix(b"data: [DONE]\n\n") + wire
    async with gateway_environment(pg_db, quota=QUOTA, respond=lambda _: httpx.Response(200, content=wire)) as env:
        await env.engine.catalog.put_model(ModelConfig(model_id="public", identity="identity-A", enabled=True, **RATES))
        identity = IdentityService(pg_db, b"p" * 32)
        if override:
            await identity.update_policy(env.key_id, 1, KeyPolicy(
                model_ids={"public"}, protocols={"openai"}, total_micro=QUOTA,
                model_overrides=(ModelRate(model_id="public", **RATES),)))
        principal = await identity.authenticate(env.secret, "openai")
        prepared = await env.engine.prepare(principal, OpenAIChatCodec().decode(BODY, {}), env.engine.meta(BODY))
        try:
            await env.engine.catalog.put_model(ModelConfig(model_id="public", identity="identity-A", enabled=True,
                                                          input_micro=9, output_micro=9, cache_read_micro=0, cache_write_micro=0))
            await identity.update_policy(env.key_id, principal.policy_version, KeyPolicy(
                model_ids={"public"}, protocols={"openai"}, total_micro=QUOTA,
                model_overrides=(ModelRate(model_id="public", input_micro=9, output_micro=9),)))
            await prepared.collect()
        finally:
            await prepared.close()
        assert await bucket(pg_db, env.key_id) == (1_060_000_000, 0)
        async with pg_db.sessions() as session:
            evidence = await session.scalar(text("SELECT usage FROM attempts"))
        assert Usage(**evidence) == Usage(1000, 200, cached_read=600)


async def test_cached_settlement_stays_in_utc_admission_windows(pg_db):
    async with gateway_environment(pg_db, quota=QUOTA) as env:
        before = datetime(2026, 9, 30, 23, 59, 50, tzinfo=timezone.utc)
        ledger = Ledger(pg_db, clock=lambda: before)
        request = replace(await admission_for(env, **RATES), deadline=before + timedelta(seconds=60))
        await ledger.reserve(request)
        await ledger.mark_dispatched(request.request_id, uuid4())
        await ledger.mark_pending(request.request_id, "interrupted")
        later = Ledger(pg_db, clock=lambda: before + timedelta(hours=25))
        await later.settle(request.request_id, Usage(1000, 200, cached_read=600, cached_write=100))
        async with pg_db.sessions() as session:
            rows = (await session.execute(text("SELECT window_kind,period,spent,held FROM quota_buckets"))).all()
        assert set(rows) == {("total", "all", 1_085_000_000, 0),
                             ("day", "2026-09-30", 1_085_000_000, 0),
                             ("month", "2026-09", 1_085_000_000, 0)}


async def test_cache_rate_overflow_rolls_back_reserve_resize_and_settle(pg_db):
    async with gateway_environment(pg_db, quota=QUOTA) as env:
        request = replace(await admission_for(env, input_micro=0, output_micro=0, cache_write_micro=10**37),
                          bounds=Bounds(10, 0))
        with pytest.raises(ValueError, match="quota_overflow"):
            await env.ledger.reserve(request)
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM requests")) == 0
            assert await session.scalar(text("SELECT count(*) FROM quota_buckets")) == 0
        await IdentityService(pg_db, b"p" * 32).update_policy(env.key_id, 1, KeyPolicy(
            model_ids={"public"}, protocols={"openai"}, total_micro=None))
        request = replace(await admission_for(env, input_micro=0, output_micro=0, cache_write_micro=10**37),
                          bounds=Bounds(1, 0))
        hold = await env.ledger.reserve(request)
        with pytest.raises(ValueError, match="quota_overflow"):
            await env.ledger.resize(hold, Bounds(10, 0))
        await env.ledger.mark_dispatched(request.request_id, uuid4())
        with pytest.raises(ValueError, match="quota_overflow"):
            await env.ledger.settle(request.request_id, Usage(10, 0, cached_write=10))
        assert await bucket(pg_db, env.key_id) == (0, 10**37)
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 0
            assert (await session.execute(text("SELECT state,input_bound,usage FROM requests"))).one() == ("dispatched", 1, None)


async def test_overbound_cache_usage_keeps_actual_charge_and_quarantines_binding(pg_db):
    wire = ("data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 2000, "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 600}}}) + "\n\ndata: [DONE]\n\n").encode()
    wire = chat_success(False).removesuffix(b"data: [DONE]\n\n") + wire
    async with gateway_environment(pg_db, quota=QUOTA, respond=lambda _: httpx.Response(200, content=wire)) as env:
        await env.engine.catalog.put_model(ModelConfig(model_id="public", identity="identity-A", enabled=True, **RATES))
        response = await env.client.post("/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer " + env.secret})
        assert response.status_code == 200, response.text
        assert await bucket(pg_db, env.key_id) == (1_850_000_000, 0)
        async with pg_db.sessions() as session:
            row = (await session.execute(text("SELECT usage,reason FROM requests"))).one()
            assert await session.scalar(text("SELECT config->'enabled' FROM model_bindings")) is False
        assert row.reason == "usage_exceeded_bound"
        assert (row.usage["computed_micro"], row.usage["charged_micro"], row.usage["input_tokens"]) == (2_060_000_000, 1_850_000_000, 2000)


async def test_management_routes_preserve_exact_cache_rates_and_accounting(pg_db):
    from .test_admin import admin_env
    async with gateway_environment(pg_db, quota=QUOTA) as env:
        request = await admission_for(env, **RATES)
        await env.ledger.reserve(request)
        await env.ledger.mark_dispatched(request.request_id, uuid4())
        await env.ledger.settle(request.request_id, Usage(1000, 200, cached_read=600, cached_write=100))
        async with admin_env(pg_db) as (client, _):
            client.headers.update({"x-test-session": "synthetic-session", "x-csrf-token": "synthetic-csrf"})
            created = await client.post("/api/service/models", json={"model_id": "precise", "identity": "precise",
                "cache_read_micro": "100000000000000123456", "cache_write_micro": "0"})
            assert created.status_code == 201, created.text
            models = (await client.get("/api/service/models")).json()
            precise = next(model for model in models if model["model_id"] == "precise")
            assert precise["cache_read_micro"] == "100000000000000123456"
            assert precise["cache_write_micro"] == "0"
            updated = await client.patch(f"/api/service/keys/{env.key_id}", json={"version": 1,
                "policy": {"model_overrides": [{"model_id": "public", "input_micro": "1", "output_micro": "2",
                    "cache_read_micro": "0", "cache_write_micro": "100000000000000123456"}]}})
            assert updated.status_code == 200, updated.text
            keys = (await client.get("/api/service/keys")).json()
            key = next(key for key in keys if key["key_id"] == str(env.key_id))
            assert key["policy"]["model_overrides"][0]["cache_read_micro"] == "0"
            assert key["policy"]["model_overrides"][0]["cache_write_micro"] == "100000000000000123456"
            response = await client.get("/api/service/usage")
            assert response.status_code == 200, response.text
            rows = response.json()
            assert rows[0]["usage"]["computed_micro"] == "1085000000"
            assert rows[0]["usage"]["charged_micro"] == "1085000000"
