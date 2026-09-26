from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from random import Random
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from autobuild_json.gateway.accounts.quota_records import CodexQuotaSnapshot, QuotaWindow
from autobuild_json.gateway.metering.records import Bounds
from autobuild_json.gateway.routing.pool_records import AccountCandidate, AccountPoolPolicy, PoolMember, PoolScope
from autobuild_json.gateway.routing.records import RouteSnapshot

NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


def candidate(number, used=None, **changes):
    credential = UUID(int=number)
    route = RouteSnapshot(credential, UUID(int=999), credential, "public", "upstream", "codex_oauth",
                          "https://fixture.invalid", "oauth", 1, frozenset({"text"}), Bounds(100, 100),
                          None, None, 0, 1, 1, 30)
    quota = None if used is None else CodexQuotaSnapshot(
        credential_id=credential, version=1, fetched_at=NOW,
        primary=QuotaWindow(used_percent=Decimal(used)))
    return AccountCandidate(route=route, quota=quota, health="active", **changes)


def policy(*items, **changes):
    return AccountPoolPolicy(members=tuple(PoolMember(credential_id=item.route.credential_id) for item in items),
                             **changes)


def ordered(config, items, rng=None):
    from autobuild_json.gateway.routing.account_pool import order_candidates
    return [route.credential_id.int for route in order_candidates(config, items, NOW, rng or Random(4))]


def test_no_policy_preserves_catalog_order_and_does_not_invent_authorization():
    a, b = candidate(2), candidate(1)
    assert ordered(None, (a, b)) == [2, 1]
    assert ordered(policy(a, candidate(3)), (a, b)) == [2]
    assert ordered(policy(a), ()) == []


def test_auto_uses_minimum_remaining_then_plan_then_expiry_then_rotating_catalog_order():
    a, b, c, unknown = candidate(1, "20"), candidate(2, "30"), candidate(3, "30"), candidate(4)
    a = a.model_copy(update={"quota": a.quota.model_copy(update={
        "secondary": QuotaWindow(used_percent=Decimal("95"))})})
    b = b.model_copy(update={"quota": b.quota.model_copy(update={"plan_type": "plus"})})
    c = c.model_copy(update={"quota": c.quota.model_copy(update={"plan_type": "pro"})})
    config = policy(a, b, c, unknown, plan_order=("pro", "plus"))
    assert ordered(config, (unknown, a, b, c)) == [3, 2, 1, 4]
    a, b = candidate(1, "20"), candidate(2, "20", subscription_expires_at=NOW + timedelta(days=1))
    assert ordered(policy(a, b), (b, a)) == [2, 1]
    assert ordered(policy(a, b, prefer_expiring=True), (a, b)) == [2, 1]


def test_priority_backup_and_single_never_promote_nonmembers():
    a, b, backup, stranger = [candidate(n) for n in range(1, 5)]
    config = AccountPoolPolicy(mode="priority", members=(
        PoolMember(credential_id=a.route.credential_id, priority=20),
        PoolMember(credential_id=b.route.credential_id, priority=10),
        PoolMember(credential_id=backup.route.credential_id, priority=0, backup=True)))
    assert ordered(config, (a, backup, stranger, b)) == [2, 1, 3]
    config = config.model_copy(update={"mode": "single", "single_credential_id": a.route.credential_id})
    assert ordered(config, (b, backup, a)) == [1]
    assert ordered(config, (b, backup)) == []


def test_random_injected_rng_and_weight_without_replacement_keep_backup_last():
    from autobuild_json.gateway.routing.account_pool import order_candidates
    items = tuple(candidate(n) for n in range(1, 5))
    config = policy(*items, mode="random")
    assert ordered(config, items, Random(4)) == [3, 1, 4, 2]
    assert ordered(config, items, Random(4)) == [3, 1, 4, 2]
    class Draws:
        def __init__(self):
            self.values = iter((0.5, 0.0, 0.0, 0.0))
        def random(self):
            return next(self.values)
    config = AccountPoolPolicy(mode="weight", members=(
        PoolMember(credential_id=items[0].route.credential_id),
        PoolMember(credential_id=items[1].route.credential_id, weight=10),
        PoolMember(credential_id=items[2].route.credential_id),
        PoolMember(credential_id=items[3].route.credential_id, weight=10000, backup=True)))
    assert [r.credential_id.int for r in order_candidates(config, items, NOW, Draws())] == [2, 1, 3, 4]


@pytest.mark.parametrize("mutation", ["health", "cooldown", "limit", "primary_full", "secondary_full", "foreign_quota"])
def test_excludes_unhealthy_cooldown_exhausted_and_mismatched_evidence(mutation):
    item = candidate(1, "20")
    if mutation == "health":
        item = item.model_copy(update={"health": "reauth_required"})
    elif mutation == "cooldown":
        item = item.model_copy(update={"cooldown_until": NOW + timedelta(seconds=1)})
    else:
        change = {"limit_reached": True} if mutation == "limit" else (
            {"credential_id": uuid4()} if mutation == "foreign_quota" else
            {mutation.removesuffix("_full"): QuotaWindow(used_percent=Decimal("100"), reset_at=NOW - timedelta(seconds=1))})
        item = item.model_copy(update={"quota": item.quota.model_copy(update=change)})
    assert ordered(policy(item), (item,)) == []


@pytest.mark.parametrize("mutation", ["missing", "stale", "future", "failed", "primary_missing", "primary_unknown",
                                      "primary_low", "secondary_missing", "secondary_low"])
def test_reserve_is_opt_in_and_fails_closed_on_missing_stale_failed_or_low_quota(mutation):
    item = candidate(1, "20")
    quota = item.quota.model_copy(update={"secondary": QuotaWindow(used_percent=Decimal("20"))})
    changes = {"missing": None, "stale": {"fetched_at": NOW - timedelta(seconds=121)},
               "future": {"fetched_at": NOW + timedelta(seconds=1)},
               "failed": {"last_error": "codex_usage_unavailable"}, "primary_missing": {"primary": None},
               "primary_unknown": {"primary": QuotaWindow()},
               "primary_low": {"primary": QuotaWindow(used_percent=Decimal("81"))},
               "secondary_missing": {"secondary": None},
               "secondary_low": {"secondary": QuotaWindow(used_percent=Decimal("81"))}}
    item = item.model_copy(update={"quota": quota.model_copy(update=changes[mutation]) if changes[mutation] else None})
    assert ordered(policy(item), (item,)) == [1]
    assert ordered(policy(item, reserve_enabled=True, min_primary_remaining=Decimal("20"),
                          min_secondary_remaining=Decimal("20")), (item,)) == []


def test_elapsed_reset_and_expired_snapshot_do_not_restore_exhausted_quota():
    stale = candidate(1, "100")
    stale = stale.model_copy(update={"quota": stale.quota.model_copy(update={
        "fetched_at": NOW - timedelta(days=1),
        "primary": QuotaWindow(used_percent=Decimal("100"), reset_at=NOW - timedelta(seconds=1))})})
    known = candidate(2, "99")
    assert ordered(policy(stale, known), (stale, known)) == [2]
    assert ordered(policy(stale, reserve_enabled=True), (stale,)) == []
    assert ordered(policy(candidate(3), reserve_enabled=True), (candidate(3),)) == []


@pytest.mark.parametrize("mode", ["auto", "random", "single", "priority", "weight"])
@pytest.mark.parametrize("reserve", [False, True])
@pytest.mark.parametrize("evidence", ["limit", "primary", "secondary"])
@pytest.mark.parametrize("state", ["expired", "failed_refresh", "expired_failed_refresh"])
def test_saved_exhaustion_requires_fresh_recovery_in_every_mode(mode, reserve, evidence, state):
    from autobuild_json.gateway.routing.account_pool import candidate_diagnostic
    item = candidate(1, "20")
    quota = {"secondary": QuotaWindow(used_percent=Decimal("20")),
             "fetched_at": NOW - timedelta(seconds=121) if "expired" in state else NOW,
             "last_error": "codex_usage_unavailable" if "failed_refresh" in state else None}
    if evidence == "limit":
        quota["limit_reached"] = True
    else:
        quota[evidence] = QuotaWindow(used_percent=Decimal("100"), reset_at=NOW - timedelta(seconds=1))
    item = item.model_copy(update={"quota": item.quota.model_copy(update=quota)})
    config = policy(item, mode=mode, reserve_enabled=reserve, min_primary_remaining=Decimal("10"),
                    min_secondary_remaining=Decimal("10"),
                    single_credential_id=item.route.credential_id if mode == "single" else None)
    assert candidate_diagnostic(config, item, NOW) == "pool_quota_exhausted"
    assert ordered(config, (item,)) == []

    recovered = item.model_copy(update={"quota": item.quota.model_copy(update={
        "version": item.quota.version + 1, "fetched_at": NOW, "last_error": None, "limit_reached": False,
        "primary": QuotaWindow(used_percent=Decimal("20")),
        "secondary": QuotaWindow(used_percent=Decimal("20"))})})
    assert candidate_diagnostic(config, recovered, NOW) is None
    assert ordered(config, (recovered,)) == [1]


def test_duplicate_authorized_bindings_do_not_duplicate_account_weight():
    a, b = candidate(1), candidate(2)
    duplicate = a.model_copy(update={"route": replace(a.route, binding_id=UUID(int=99))})
    assert ordered(policy(a, b), (duplicate, a, b)) == [1, 2]


@pytest.mark.parametrize("weight", [0, -1, True, 1.5, 10001])
def test_invalid_weights_rejected(weight):
    with pytest.raises(ValidationError):
        PoolMember(credential_id=uuid4(), weight=weight)


def test_duplicate_members_and_retry_budget_rejected():
    member = PoolMember(credential_id=uuid4())
    with pytest.raises(ValidationError):
        AccountPoolPolicy(members=(member, member))
    with pytest.raises(ValidationError):
        AccountPoolPolicy(mode="random", members=(), retry_limit=8)


def test_all_accounts_is_explicit_dynamic_and_does_not_unpause_empty_manual_pool():
    items = tuple(candidate(n) for n in range(1, 1001))
    assert ordered(AccountPoolPolicy(), items) == []
    config = AccountPoolPolicy(mode="round_robin", all_accounts=True)
    assert ordered(config, items) == list(range(1, 1001))
    assert ordered(config, items[225:] + items[:225]) == list(range(226, 1001)) + list(range(1, 226))
    assert ordered(config, (candidate(1001),)) == [1001]
    with pytest.raises(ValidationError):
        AccountPoolPolicy(mode="single", all_accounts=True, members=(PoolMember(credential_id=items[0].route.credential_id),),
                          single_credential_id=items[0].route.credential_id)


@pytest.mark.parametrize("bad", ["a\nb", "a\x00b", "a\x7fb", "a\u200bb"])
def test_scope_model_and_candidate_health_reject_nonprintable_text(bad):
    with pytest.raises(ValidationError):
        PoolScope(customer_id=uuid4(), key_id=uuid4(), model_id=bad)
    with pytest.raises(ValidationError):
        AccountCandidate(route=candidate(1).route, health=bad)
