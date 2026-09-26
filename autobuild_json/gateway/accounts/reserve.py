"""Reserve is a distinct upstream entitlement, not the pool's quota floor."""
from datetime import timedelta

RESERVE_MODEL = "gpt-reserve"


def normalize_limit_name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or not value.isprintable():
        raise ValueError("invalid_limit_name")
    return value.strip().lower().replace("_", "-").replace(" ", "-")


def is_reserve_name(value):
    key = normalize_limit_name(value)
    return key in {RESERVE_MODEL, "gptreserve"} or key.startswith(("gpt-reserve-", "gptreserve-"))


def reserve_diagnostic(quota, credential_id, now, max_age=120):
    """No fallback to ordinary Luna; stale/absent/contradictory evidence denies."""
    if (quota is None or quota.credential_id != credential_id or quota.last_error is not None
            or not now - timedelta(seconds=max_age) <= quota.fetched_at <= now):
        return "pool_reserve_unknown"
    limits = [item for item in quota.additional_rate_limits if item.model_id == RESERVE_MODEL]
    if quota.allowed is not False or quota.banner_type != "luna_reserve" or not limits:
        return "pool_reserve_unavailable"
    if any(item.allowed is not True or item.limit_reached is True or any(
            window is not None and window.used_percent == 100 for window in (item.primary, item.secondary))
            for item in limits):
        return "pool_reserve_unavailable"
    return None
