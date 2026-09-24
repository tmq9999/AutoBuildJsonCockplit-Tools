import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from autobuild_json.gateway.errors import GatewayError

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def deadline():
    return datetime.now(timezone.utc) + timedelta(seconds=25)


async def account(db):
    from autobuild_json.oauth import ISSUER
    from autobuild_json.gateway.routing.records import ProviderConfig
    credential, provider = uuid4(), uuid4()
    config = ProviderConfig(name="fixture", adapter="codex_oauth", auth_mode="oauth",
                            root="https://chatgpt.com/backend-api/codex", wire_api="responses")
    async with db.sessions.begin() as session:
        await session.execute(text("INSERT INTO providers(id,config) VALUES (:id,CAST(:config AS jsonb))"),
                              {"id": provider, "config": config.model_dump_json()})
        await session.execute(text("INSERT INTO credentials(id,provider_id,encrypted_secret,issuer,account_id) "
                                   "VALUES (:id,:p,'{}',:issuer,:account)"),
                              {"id": credential, "p": provider, "issuer": ISSUER, "account": str(credential)})
    return credential


def snapshots(credential, version=1, count=2):
    from decimal import Decimal
    from autobuild_json.gateway.accounts.quota_records import CodexQuotaSnapshot, QuotaWindow, ResetCreditsSnapshot
    now = datetime.now(timezone.utc)
    return (CodexQuotaSnapshot(credential_id=credential, version=version, fetched_at=now,
                              primary=QuotaWindow(used_percent=Decimal("42"))),
            ResetCreditsSnapshot(credential_id=credential, version=version, fetched_at=now, available_count=count))


async def seeded(db):
    from autobuild_json.gateway.accounts.quota_store import QuotaStore
    credential = await account(db)
    store = QuotaStore(db)
    claim = await store.begin_refresh(credential, deadline())
    assert await store.save_refresh(claim, *snapshots(credential), ())
    return store, credential, await store.read_credits(credential)


async def test_refresh_generation_rejects_older_save(pg_db):
    from autobuild_json.gateway.accounts.quota_store import QuotaStore
    credential = await account(pg_db)
    store = QuotaStore(pg_db)
    first = await store.begin_refresh(credential, deadline())
    second = await store.begin_refresh(credential, deadline())
    assert second.generation > first.generation
    assert await store.save_refresh(second, *snapshots(credential, count=3), ())
    assert not await store.save_refresh(first, *snapshots(credential, count=1), ())
    assert (await store.read_credits(credential)).available_count == 3


async def test_partial_refresh_preserves_successes_and_safe_errors(pg_db):
    store, credential, credits = await seeded(pg_db)
    claim = await store.begin_refresh(credential, deadline())
    assert await store.save_refresh(claim, None, None, ("codex_usage_unavailable", "codex_credits_unavailable"))
    assert (await store.read_credits(credential)) == credits
    assert (await store.read(credential)).primary.used_percent == 42
    async with pg_db.sessions() as session:
        row = (await session.execute(text("SELECT failure_count,last_error,refresh_deadline FROM codex_account_state"))).one()
    assert row == (1, "codex_usage_unavailable", None)
    claim = await store.begin_refresh(credential, deadline())
    with pytest.raises(GatewayError, match="invalid_request"):
        await store.save_refresh(claim, None, None, ("secret raw error",))


@pytest.mark.parametrize("change", ["UPDATE credentials SET token_generation=token_generation+1",
                                    "UPDATE credentials SET version=version+1",
                                    "UPDATE providers SET version=version+1"])
async def test_refresh_rejects_dependency_changes(pg_db, change):
    store, credential, _ = await seeded(pg_db)
    claim = await store.begin_refresh(credential, deadline())
    async with pg_db.sessions.begin() as session:
        await session.execute(text(change))
    assert not await store.save_refresh(claim, *snapshots(credential), ())


async def test_reset_request_replay_and_payload_mismatch(pg_db):
    store, credential, credits = await seeded(pg_db)
    request_id = uuid4()
    first = await store.prepare_reset(credential, request_id, credits.version, "admin", deadline=deadline())
    replay = await store.prepare_reset(credential, request_id, credits.version, "other", deadline=deadline())
    assert replay.result == first.result and replay.is_new is False
    assert replay.redeem_request_id == first.redeem_request_id != request_id
    assert "redeem_request_id" not in first.result.model_dump()
    with pytest.raises(GatewayError, match="payload_mismatch") as exc:
        await store.prepare_reset(credential, request_id, credits.version + 1, "admin", deadline=deadline())
    assert exc.value.status == 409
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='codex.reset.prepared'")) == 1


async def test_concurrent_confirmations_and_cross_account_id_race(pg_db):
    store, credential, credits = await seeded(pg_db)
    results = await asyncio.gather(*(store.prepare_reset(credential, uuid4(), credits.version, "admin", deadline=deadline())
                                     for _ in range(2)), return_exceptions=True)
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert [result.code for result in results if isinstance(result, GatewayError)] == ["operation_conflict"]
    other_store, other, other_credits = await seeded(pg_db)
    third_store, third, third_credits = await seeded(pg_db)
    request_id = uuid4()
    results = await asyncio.gather(other_store.prepare_reset(other, request_id, other_credits.version, "admin", deadline=deadline()),
                                   third_store.prepare_reset(third, request_id, third_credits.version, "admin", deadline=deadline()),
                                   return_exceptions=True)
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert [result.code for result in results if isinstance(result, GatewayError)] == ["payload_mismatch"]


async def test_dispatch_cas_and_unknown_survives_fresh_credits(pg_db):
    store, credential, credits = await seeded(pg_db)
    claim = await store.prepare_reset(credential, uuid4(), credits.version, "admin", deadline=deadline())
    assert await store.mark_dispatched(claim.result.operation_id, claim.generation)
    assert not await store.mark_dispatched(claim.result.operation_id, claim.generation)
    result = await store.finish_reset(claim.result.operation_id, claim.generation, "unknown", "codex_reset_uncertain", 503)
    refresh = await store.begin_refresh(credential, deadline())
    assert await store.save_refresh(refresh, *snapshots(credential), ())
    with pytest.raises(GatewayError, match="operation_conflict"):
        await store.prepare_reset(credential, uuid4(), (await store.read_credits(credential)).version, "admin", deadline=deadline())
    with pytest.raises(GatewayError, match="claim_lost"):
        await store.finish_reset(claim.result.operation_id, claim.generation, "rejected", "upstream_error", 400)
    assert result.state == "unknown"


@pytest.mark.parametrize("dispatched,expected", [(False, "rejected"), (True, "unknown")])
async def test_recovery_fences_expired_operations(pg_db, dispatched, expected):
    store, credential, credits = await seeded(pg_db)
    claim = await store.prepare_reset(credential, uuid4(), credits.version, "admin", deadline=deadline())
    if dispatched:
        assert await store.mark_dispatched(claim.result.operation_id, claim.generation)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE codex_reset_requests SET requested_at=clock_timestamp()-interval '2 minutes', "
                                   "deadline=clock_timestamp()-interval '1 minute'"))
    assert await store.recover_expired() == 1
    assert await store.recover_expired() == 0
    assert not await store.mark_dispatched(claim.result.operation_id, claim.generation)
    replay = await store.prepare_reset(credential, claim.result.operation_id, credits.version, "admin", deadline=deadline())
    assert replay.result.state == expected and replay.generation > claim.generation


@pytest.mark.parametrize("outcome,expected", [("confirmed_applied", "succeeded"), ("confirmed_not_applied", "rejected")])
async def test_resolution_cas_audit_and_invalidates_consumable_credits(pg_db, outcome, expected):
    store, credential, credits = await seeded(pg_db)
    claim = await store.prepare_reset(credential, uuid4(), credits.version, "admin", deadline=deadline())
    assert await store.mark_dispatched(claim.result.operation_id, claim.generation)
    unknown = await store.finish_reset(claim.result.operation_id, claim.generation, "unknown", "codex_reset_uncertain", None)
    with pytest.raises(GatewayError, match="version_conflict"):
        await store.resolve_reset(unknown.operation_id, unknown.version - 1, outcome, "verified evidence", "admin")
    with pytest.raises(GatewayError, match="invalid_request"):
        await store.resolve_reset(unknown.operation_id, unknown.version, outcome, "Bearer secret-token", "admin")
    resolved = await store.resolve_reset(unknown.operation_id, unknown.version, outcome, "verified evidence", "admin")
    assert resolved.state == expected and resolved.version > unknown.version
    with pytest.raises(GatewayError):
        await store.resolve_reset(unknown.operation_id, resolved.version, outcome, "changed evidence", "admin")
    with pytest.raises(GatewayError, match="codex_credits_stale"):
        await store.prepare_reset(credential, uuid4(), credits.version, "admin", deadline=deadline())
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='codex.reset.resolved'")) == 1
        assert await session.scalar(text("SELECT count(*) FROM quota_buckets")) == 0


async def test_deadline_identity_preflight_and_disabled_read(pg_db):
    store, credential, credits = await seeded(pg_db)
    with pytest.raises(GatewayError, match="invalid_request"):
        await store.begin_refresh(credential, datetime.now())
    with pytest.raises(GatewayError, match="invalid_request"):
        await store.begin_refresh(credential, deadline() + timedelta(minutes=1))
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE credentials SET enabled=false"))
    assert (await store.read_credits(credential)) == credits
    with pytest.raises(GatewayError, match="invalid_state"):
        await store.prepare_reset(credential, uuid4(), credits.version, "admin", deadline=deadline())
    with pytest.raises(GatewayError, match="not_found"):
        await store.read(uuid4())


async def test_confirmed_success_requires_post_dispatch_refresh(pg_db):
    store, credential, credits = await seeded(pg_db)
    claim = await store.prepare_reset(credential, uuid4(), credits.version, "admin", deadline=deadline())
    assert await store.mark_dispatched(claim.result.operation_id, claim.generation)
    with pytest.raises(GatewayError, match="invalid_state"):
        await store.finish_reset(claim.result.operation_id, claim.generation, "succeeded", None, 200)
    pending = await store.finish_reset(claim.result.operation_id, claim.generation, "succeeded_refresh_failed", "codex_refresh_failed", 200)
    assert pending.state == "succeeded_refresh_failed"
    refresh = await store.begin_refresh(credential, deadline())
    assert await store.save_refresh(refresh, *snapshots(credential, count=1), ())
    result = await store.finish_reset(claim.result.operation_id, claim.generation, "succeeded", None, 200)
    assert result.state == "succeeded"


async def test_receipt_must_precede_one_complete_refresh(pg_db):
    store, credential, credits = await seeded(pg_db)
    claim = await store.prepare_reset(credential, uuid4(), credits.version, "admin", deadline=deadline())
    assert await store.mark_dispatched(claim.result.operation_id, claim.generation)
    before_receipt = await store.begin_refresh(credential, deadline())
    await store.finish_reset(claim.result.operation_id, claim.generation, "succeeded_refresh_failed", "codex_refresh_failed", 200)
    assert await store.save_refresh(before_receipt, *snapshots(credential), ())
    with pytest.raises(GatewayError, match="invalid_state"):
        await store.finish_reset(claim.result.operation_id, claim.generation, "succeeded", None, 200)
    first_half = await store.begin_refresh(credential, deadline())
    usage, _ = snapshots(credential)
    assert await store.save_refresh(first_half, usage, None, ("codex_credits_unavailable",))
    second_half = await store.begin_refresh(credential, deadline())
    _, credits = snapshots(credential)
    assert await store.save_refresh(second_half, None, credits, ("codex_usage_unavailable",))
    with pytest.raises(GatewayError, match="invalid_state"):
        await store.finish_reset(claim.result.operation_id, claim.generation, "succeeded", None, 200)


async def test_preflight_rejects_token_change_and_same_confirmation_is_once_only(pg_db):
    store, credential, credits = await seeded(pg_db)
    request_id = uuid4()
    results = await asyncio.gather(*(store.prepare_reset(credential, request_id, credits.version, "admin", deadline=deadline())
                                    for _ in range(2)))
    assert sorted(result.is_new for result in results) == [False, True]
    claim = results[0]
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE credentials SET token_generation=token_generation+1"))
    assert not await store.mark_dispatched(request_id, claim.generation)
    assert (await store.get_reset(request_id)).state == "prepared"
    assert await store.get_reset(uuid4()) is None


async def test_proxy_identity_and_version_fence_refresh(pg_db):
    store, credential, _ = await seeded(pg_db)
    proxy = uuid4()
    async with pg_db.sessions.begin() as session:
        await session.execute(text("INSERT INTO proxy_profiles(id,name,config,encrypted_entries) VALUES (:id,'fixture','{}','{}')"), {"id": proxy})
        await session.execute(text("UPDATE credentials SET profile_id=:id"), {"id": proxy})
    claim = await store.begin_refresh(credential, deadline())
    assert claim.proxy_id == proxy and claim.proxy_version == 1
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE proxy_profiles SET version=version+1"))
    assert not await store.save_refresh(claim, *snapshots(credential), ())
    claim = await store.begin_refresh(credential, deadline())
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE credentials SET profile_id=NULL"))
    assert not await store.save_refresh(claim, *snapshots(credential), ())


@pytest.mark.parametrize("mutation,code", [
    ("UPDATE codex_account_state SET credits_fetched_at=clock_timestamp()-interval '121 seconds'", "codex_credits_stale"),
    ("UPDATE codex_account_state SET credits_snapshot=jsonb_set(credits_snapshot,'{available_count}','0')", "codex_no_credits"),
    ("UPDATE credentials SET issuer='https://other.invalid'", "invalid_state"),
])
async def test_new_reset_requires_fresh_positive_credits_and_oauth(pg_db, mutation, code):
    store, credential, credits = await seeded(pg_db)
    async with pg_db.sessions.begin() as session:
        await session.execute(text(mutation))
    with pytest.raises(GatewayError, match=code):
        await store.prepare_reset(credential, uuid4(), credits.version, "admin", deadline=deadline())


async def test_save_rejects_expired_claim_and_bad_error_input(pg_db):
    store, credential, _ = await seeded(pg_db)
    claim = await store.begin_refresh(credential, deadline())
    for errors in (({},), (None,), ("",)):
        with pytest.raises(GatewayError, match="invalid_request"):
            await store.save_refresh(claim, None, None, errors)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE codex_account_state SET refresh_deadline=clock_timestamp()-interval '1 second'"))
    assert not await store.save_refresh(claim, *snapshots(credential), ())


async def test_terminal_success_cannot_override_receipt_status_or_use_stale_snapshots(pg_db):
    store, credential, credits = await seeded(pg_db)
    claim = await store.prepare_reset(credential, uuid4(), credits.version, "admin", deadline=deadline())
    assert await store.mark_dispatched(claim.result.operation_id, claim.generation)
    await store.finish_reset(claim.result.operation_id, claim.generation, "succeeded_refresh_failed", "codex_refresh_failed", 201)
    refresh = await store.begin_refresh(credential, deadline())
    assert await store.save_refresh(refresh, *snapshots(credential), ())
    with pytest.raises(GatewayError, match="invalid_state"):
        await store.finish_reset(claim.result.operation_id, claim.generation, "succeeded", None, 503)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE codex_account_state SET fetched_at=clock_timestamp()-interval '121 seconds'"))
    with pytest.raises(GatewayError, match="invalid_state"):
        await store.finish_reset(claim.result.operation_id, claim.generation, "succeeded", None, 201)


async def test_success_revalidates_snapshot_dependencies_and_resolution_counter_types(pg_db):
    store, credential, credits = await seeded(pg_db)
    claim = await store.prepare_reset(credential, uuid4(), credits.version, "admin", deadline=deadline())
    assert await store.mark_dispatched(claim.result.operation_id, claim.generation)
    pending = await store.finish_reset(claim.result.operation_id, claim.generation, "succeeded_refresh_failed", "codex_refresh_failed", 200)
    with pytest.raises(GatewayError, match="invalid_request"):
        await store.resolve_reset(pending.operation_id, True, "confirmed_applied", "verified evidence", "admin")
    refresh = await store.begin_refresh(credential, deadline())
    assert await store.save_refresh(refresh, *snapshots(credential), ())
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE credentials SET version=version+1"))
    with pytest.raises(GatewayError, match="claim_lost"):
        await store.finish_reset(claim.result.operation_id, claim.generation, "succeeded", None, 200)
