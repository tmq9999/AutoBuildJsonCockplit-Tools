"""Pure ordering of already-authorized routes; quota never grants permission."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from random import SystemRandom

from .pool_records import AccountCandidate, AccountPoolPolicy
from .records import RouteSnapshot


def _fresh(policy, candidate, now):
    quota = candidate.quota
    return (quota is not None and quota.last_error is None
            and now - timedelta(seconds=policy.snapshot_max_age_seconds) <= quota.fetched_at <= now)


def _remaining(window):
    return None if window is None or window.used_percent is None else Decimal(100) - window.used_percent


def candidate_diagnostic(policy: AccountPoolPolicy, candidate: AccountCandidate, now: datetime) -> str | None:
    """A fixed exclusion code, or None; never includes upstream text or identity."""
    member = next((m for m in policy.members if m.credential_id == candidate.route.credential_id), None)
    if member is None or candidate.route.adapter != "codex_oauth":
        return "pool_not_member"
    if policy.mode == "single" and member.credential_id != policy.single_credential_id:
        return "pool_not_selected"
    if candidate.health != "active":
        return "pool_unhealthy"
    if candidate.cooldown_until is not None and candidate.cooldown_until > now:
        return "pool_cooldown"
    quota = candidate.quota
    if quota is not None and quota.credential_id != candidate.route.credential_id:
        return "pool_quota_mismatch"
    if not _fresh(policy, candidate, now):
        return "pool_reserve_unknown" if policy.reserve_enabled else None
    remaining = (_remaining(quota.primary), _remaining(quota.secondary))
    if quota.limit_reached or any(value == 0 for value in remaining):
        return "pool_quota_exhausted"
    if policy.reserve_enabled:
        for value, threshold in zip(remaining, (policy.min_primary_remaining, policy.min_secondary_remaining)):
            if threshold > 0 and value is None:
                return "pool_reserve_unknown"
            if value is not None and value < threshold:
                return "pool_reserve_low"
    return None


def order_candidates(policy: AccountPoolPolicy | None, candidates, now: datetime, rng=None) -> tuple[RouteSnapshot, ...]:
    """No policy preserves Catalog order; configured pools visit each account once.

    RNG is per-call system randomness by default. Tests may inject a Random-like
    object. reset_at is not a quota refresh and never alters the observation.
    """
    if policy is None:
        return tuple(candidate.route for candidate in candidates)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone_required")
    members = {member.credential_id: member for member in policy.members}
    unique = {}
    for candidate in candidates:
        if candidate_diagnostic(policy, candidate, now) is None:
            unique.setdefault(candidate.route.credential_id, candidate)
    rng = rng if rng is not None else SystemRandom()
    plans = {plan: index for index, plan in enumerate(policy.plan_order)}

    def auto_key(candidate):
        quota = candidate.quota if _fresh(policy, candidate, now) else None
        known = [] if quota is None else [value for window in (quota.primary, quota.secondary)
                                         if (value := _remaining(window)) is not None]
        return (not known, -min(known) if known else Decimal(0),
                plans.get(quota.plan_type, len(plans)) if quota else len(plans),
                candidate.subscription_expires_at if policy.prefer_expiring and candidate.subscription_expires_at
                else datetime.max.replace(tzinfo=timezone.utc), candidate.route.credential_id.int)

    result = []
    for backup in (False, True):
        group = [candidate for identity, candidate in unique.items() if members[identity].backup == backup]
        if policy.mode == "auto":
            group.sort(key=auto_key)
        elif policy.mode == "priority":
            group.sort(key=lambda candidate: (members[candidate.route.credential_id].priority,
                                               candidate.route.credential_id.int))
        elif policy.mode == "random":
            rng.shuffle(group)
        elif policy.mode == "weight":
            weighted = []
            while group:
                target = rng.random() * sum(members[c.route.credential_id].weight for c in group)
                selected = len(group) - 1
                for index, candidate in enumerate(group):
                    target -= members[candidate.route.credential_id].weight
                    if target < 0:
                        selected = index
                        break
                weighted.append(group.pop(selected))
            group = weighted
        result.extend(candidate.route for candidate in group)
    return tuple(result)
