import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def ledger_case(pg_db, **policy_values):
    from autobuild_json.gateway.identity.service import IdentityService
    from autobuild_json.gateway.identity.policy import KeyPolicy
    from autobuild_json.gateway.metering.ledger import Ledger
    from autobuild_json.gateway.metering.records import Admission, Bounds
    identity = IdentityService(pg_db, pepper=b"p" * 32)
    owner = await identity.create_customer("Ledger test")
    policy = KeyPolicy(**dict({"model_ids": {"m"}, "protocols": {"openai"}, "total_micro": 1000,
                              "concurrency": 10}, **policy_values))
    issued = await identity.create_key(owner, policy)
    principal = await identity.authenticate(issued.secret, "openai")
    def admission(amount):
        return Admission(uuid4(), principal, "m", Bounds(amount, 0), 1, 1,
                         datetime.now(timezone.utc) + timedelta(seconds=60))
    return Ledger(pg_db), admission, issued, identity


async def bucket(pg_db, key_id):
    from sqlalchemy import text
    async with pg_db.sessions() as session:
        row = (await session.execute(text("SELECT spent, held FROM quota_buckets WHERE key_id=:id AND window_kind='total'"),
                                      {"id": key_id})).mappings().one()
        return int(row["spent"]), int(row["held"])


async def test_racing_reservations_cannot_overspend(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    ledger, admission, issued, _ = await ledger_case(pg_db)
    results = await asyncio.gather(ledger.reserve(admission(700)), ledger.reserve(admission(700)),
                                   return_exceptions=True)
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert sum(isinstance(r, GatewayError) and r.code == "quota_exceeded" for r in results) == 1
    assert await bucket(pg_db, issued.key_id) == (0, 700)


async def test_settlement_is_idempotent_and_refunds_unused_hold(pg_db):
    from autobuild_json.gateway.metering.records import Usage
    ledger, admission, issued, _ = await ledger_case(pg_db)
    request = admission(700)
    await ledger.reserve(request)
    await ledger.mark_dispatched(request.request_id, uuid4())
    await asyncio.gather(ledger.settle(request.request_id, Usage(100, 50)),
                         ledger.settle(request.request_id, Usage(100, 50)))
    assert await bucket(pg_db, issued.key_id) == (150, 0)


async def test_missing_usage_keeps_hold_and_rejects_unproven_refund(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    ledger, admission, issued, _ = await ledger_case(pg_db)
    request = admission(700)
    await ledger.reserve(request)
    await ledger.mark_dispatched(request.request_id, uuid4())
    await ledger.mark_pending(request.request_id, "usage_missing")
    with pytest.raises(GatewayError, match="invalid_state"):
        await ledger.release_unspent(request.request_id, "not_dispatched")
    assert await bucket(pg_db, issued.key_id) == (0, 700)
    await ledger.adjust(request.request_id, 50, "admin", "provider reconciliation")
    assert await bucket(pg_db, issued.key_id) == (50, 0)


async def test_snapshot_coefficient_survives_policy_edit(pg_db):
    from autobuild_json.gateway.identity.policy import KeyPolicy
    from autobuild_json.gateway.metering.records import Usage
    ledger, admission, issued, identity = await ledger_case(pg_db)
    request = replace(admission(100), input_micro=3)
    await ledger.reserve(request)
    await ledger.mark_dispatched(request.request_id, uuid4())
    await identity.update_policy(issued.key_id, issued.version,
        KeyPolicy(model_ids={"m"}, protocols={"openai"}, total_micro=1000))
    await ledger.settle(request.request_id, Usage(10, 0))
    assert await bucket(pg_db, issued.key_id) == (30, 0)


async def test_stale_principal_is_rejected_before_admission(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    ledger, admission, issued, identity = await ledger_case(pg_db)
    await identity.rotate(issued.key_id, issued.version)
    with pytest.raises(GatewayError, match="invalid_api_key"):
        await ledger.reserve(admission(10))


async def test_duplicate_request_claim_does_not_reserve_twice(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    ledger, admission, issued, _ = await ledger_case(pg_db)
    request = replace(admission(200), idempotency_digest=b"i" * 32, payload_digest=b"p" * 32)
    await ledger.reserve(request)
    with pytest.raises(GatewayError, match="duplicate_request"):
        await ledger.reserve(replace(request, request_id=uuid4()))
    assert await bucket(pg_db, issued.key_id) == (0, 200)


async def test_zero_quota_rejects_but_explicit_unlimited_accepts(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    ledger, admission, _, _ = await ledger_case(pg_db, total_micro=0)
    with pytest.raises(GatewayError, match="quota_exceeded"):
        await ledger.reserve(admission(1))
    ledger, admission, issued, _ = await ledger_case(pg_db, total_micro=None)
    await ledger.reserve(admission(10000))
    assert await bucket(pg_db, issued.key_id) == (0, 10000)


async def test_concurrency_and_rpm_checked_under_same_lock(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    ledger, admission, _, _ = await ledger_case(pg_db, concurrency=1)
    request = admission(1)
    await ledger.reserve(request)
    with pytest.raises(GatewayError, match="concurrency_limit"):
        await ledger.reserve(admission(1))
    await ledger.release_unspent(request.request_id, "not_dispatched")
    await ledger.reserve(admission(1))
    ledger, admission, _, _ = await ledger_case(pg_db, rpm=1)
    await ledger.reserve(admission(1))
    with pytest.raises(GatewayError, match="rate_limited"):
        await ledger.reserve(admission(1))


async def test_resize_cannot_overbook_and_overage_is_not_charged(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.metering.records import Bounds, Usage
    ledger, admission, issued, _ = await ledger_case(pg_db)
    request = admission(900)
    hold = await ledger.reserve(request)
    with pytest.raises(GatewayError, match="quota_exceeded"):
        await ledger.resize(hold, Bounds(1100, 0))
    await ledger.mark_dispatched(request.request_id, uuid4())
    await ledger.settle(request.request_id, Usage(1200, 0))
    assert await bucket(pg_db, issued.key_id) == (900, 0)


async def test_payload_mismatch_has_distinct_duplicate_reason(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    ledger, admission, _, _ = await ledger_case(pg_db)
    request = replace(admission(1), idempotency_digest=b"i" * 32, payload_digest=b"p" * 32)
    await ledger.reserve(request)
    with pytest.raises(GatewayError) as exc:
        await ledger.reserve(replace(request, request_id=uuid4(), payload_digest=b"q" * 32))
    assert exc.value.code == "payload_mismatch"


async def test_daily_and_monthly_limits_are_independent(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    for limits in [{"day_micro": 50}, {"month_micro": 50}]:
        ledger, admission, _, _ = await ledger_case(pg_db, **limits)
        with pytest.raises(GatewayError, match="quota_exceeded"):
            await ledger.reserve(admission(51))


async def test_utc_windows_are_pinned_to_admission_not_settlement(pg_db):
    from sqlalchemy import text
    from autobuild_json.gateway.metering.ledger import Ledger
    from autobuild_json.gateway.metering.records import Usage
    _, admission, issued, _ = await ledger_case(pg_db)
    first_day = datetime(2026, 9, 30, 23, 59, 50, tzinfo=timezone.utc)
    ledger = Ledger(pg_db, clock=lambda: first_day)
    request = replace(admission(100), deadline=first_day + timedelta(seconds=60))
    await ledger.reserve(request)
    await ledger.mark_dispatched(request.request_id, uuid4())
    next_day = first_day + timedelta(seconds=20)
    later = Ledger(pg_db, clock=lambda: next_day)
    await later.settle(request.request_id, Usage(50, 0))
    async with pg_db.sessions() as session:
        rows = (await session.execute(text("SELECT window_kind,period,spent FROM quota_buckets WHERE key_id=:key"),
                                      {"key": issued.key_id})).all()
    assert ("day", "2026-09-30", 50) in rows
    assert ("month", "2026-09", 50) in rows


async def test_provider_rejection_can_release_but_unknown_timeout_cannot(pg_db):
    ledger, admission, issued, _ = await ledger_case(pg_db)
    request, attempt = admission(700), uuid4()
    await ledger.reserve(request)
    await ledger.mark_dispatched(request.request_id, attempt)
    await ledger.mark_rejected(request.request_id, attempt, "rejected_before_generation")
    await ledger.release_unspent(request.request_id, "not_dispatched")
    assert await bucket(pg_db, issued.key_id) == (0, 0)


async def test_pending_hold_survives_restart_and_24_hours(pg_db):
    from sqlalchemy import text
    from autobuild_json.gateway.metering.ledger import Ledger
    from autobuild_json.gateway.errors import GatewayError
    ledger, admission, issued, _ = await ledger_case(pg_db)
    request = admission(900)
    await ledger.reserve(request)
    await ledger.mark_dispatched(request.request_id, uuid4())
    await ledger.mark_pending(request.request_id, "interrupted")
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE requests SET admitted_at=admitted_at-interval '25 hours', "
                                   "deadline=deadline-interval '25 hours' WHERE id=:id"), {"id": request.request_id})
    with pytest.raises(GatewayError, match="quota_exceeded"):
        await Ledger(pg_db).reserve(admission(200))
    assert await bucket(pg_db, issued.key_id) == (0, 900)


async def test_storage_failure_rolls_back_partial_reservation(pg_db, monkeypatch):
    from sqlalchemy import text
    ledger, admission, _, _ = await ledger_case(pg_db)
    original = ledger._buckets
    async def failing_write(*args, **kwargs):
        await original(*args, **kwargs)
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(ledger, "_buckets", failing_write)
    with pytest.raises(OSError):
        await ledger.reserve(admission(700))
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM requests")) == 0
        assert await session.scalar(text("SELECT count(*) FROM quota_buckets")) == 0


async def test_second_retry_limit_is_durable(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    ledger, admission, _, _ = await ledger_case(pg_db)
    request = admission(1)
    await ledger.reserve(request)
    for _ in range(2):
        attempt = uuid4()
        await ledger.mark_dispatched(request.request_id, attempt)
        await ledger.mark_rejected(request.request_id, attempt, "rejected_before_generation")
    with pytest.raises(GatewayError, match="invalid_state"):
        await ledger.mark_dispatched(request.request_id, uuid4())


async def test_model_override_is_applied_atomically_at_admission(pg_db):
    from autobuild_json.gateway.identity.policy import ModelRate
    ledger, admission, issued, _ = await ledger_case(pg_db,
        model_overrides=(ModelRate(model_id="m", input_micro=3, output_micro=7),))
    await ledger.reserve(admission(100))
    assert await bucket(pg_db, issued.key_id) == (0, 300)
