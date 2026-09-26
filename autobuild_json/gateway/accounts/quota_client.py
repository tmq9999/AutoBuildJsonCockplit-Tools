"""Guarded account calls and allowlisted, unknown-preserving parsers."""
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time

from pydantic import ValidationError

from ..errors import GatewayError, UpstreamRejected
from ..providers.retry import parse_retry_after
from ..transport.http import OutboundRequest
from .quota_records import AdditionalRateLimit, CodexQuotaSnapshot, QuotaWindow, ResetCredit, ResetCreditsSnapshot
from .reserve import RESERVE_MODEL, is_reserve_name, normalize_limit_name

ACCOUNT_ROOT = 'https://chatgpt.com/backend-api/wham'


@dataclass(frozen=True)
class ResetReceipt:
    status: int
    retry_after: int | None
    received_at: datetime


def _integer(value, maximum=253402300799):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError()
    return value


def _timestamp(value):
    if type(value) is int:
        return datetime.fromtimestamp(_integer(value), timezone.utc)
    if not isinstance(value, str) or len(value) > 40 or 'T' not in value:
        raise ValueError()
    parsed = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError()
    parsed = parsed.astimezone(timezone.utc)
    if parsed.timestamp() < 0:
        raise ValueError()
    return parsed


def _alias(data, *names):
    values = [data[name] for name in names if name in data]
    if values and any(type(v) is not type(values[0]) or v != values[0] for v in values[1:]):
        raise ValueError()
    return values[0] if values else None


def _window(data, now):
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError()
    percent = data.get('used_percent')
    if percent is not None:
        if type(percent) not in {int, float, Decimal}:
            raise ValueError()
        percent = Decimal(str(percent))
    seconds = data.get('limit_window_seconds')
    if seconds is not None:
        seconds = _integer(seconds, 315360000)
    reset = data.get('reset_at')
    relative = data.get('reset_after_seconds')
    if relative is not None:
        relative = _integer(relative, 315360000)
    reset = _timestamp(reset) if reset is not None else now + timedelta(seconds=relative) if relative is not None else None
    return QuotaWindow(used_percent=percent, window_seconds=seconds, reset_at=reset)


def _additional_limits(payload, received_at):
    raw = _alias(payload, 'additional_rate_limits', 'additionalRateLimits')
    if raw is None:
        return ()
    if not isinstance(raw, (list, dict)) or len(raw) > 100:
        raise ValueError()
    entries = raw.items() if isinstance(raw, dict) else ((None, item) for item in raw)
    result, seen = [], set()
    for key, entry in entries:
        if not isinstance(entry, dict):
            raise ValueError()
        label = _alias(entry, 'limit_name', 'limitName')
        feature = _alias(entry, 'metered_feature', 'meteredFeature')
        names = [name for name in (key, label, entry.get('name'), feature) if name is not None]
        if not names:
            raise ValueError()
        reserve = any(is_reserve_name(name) for name in names)
        model = RESERVE_MODEL if reserve else normalize_limit_name(label or key or names[0])
        if model in seen:
            raise ValueError()
        seen.add(model)
        limits = _alias(entry, 'rate_limit', 'rateLimit')
        if limits is None:
            limits = {}
        if not isinstance(limits, dict):
            raise ValueError()
        allowed = limits.get('allowed', entry.get('allowed'))
        if 'allowed' in limits and 'allowed' in entry and (
                type(limits['allowed']) is not type(entry['allowed']) or limits['allowed'] != entry['allowed']):
            raise ValueError()
        result.append(AdditionalRateLimit(model_id=model, limit_name=label or key or names[0],
            metered_feature=feature, allowed=allowed, limit_reached=limits.get('limit_reached'),
            primary=_window(limits.get('primary_window'), received_at),
            secondary=_window(limits.get('secondary_window'), received_at)))
    return tuple(result)


def parse_usage(payload, credential_id, received_at):
    try:
        if not isinstance(payload, dict):
            raise ValueError()
        limits = payload.get('rate_limit')
        if limits is None:
            limits = {}
        if not isinstance(limits, dict):
            raise ValueError()
        allowed = limits.get('allowed')
        if allowed is not None and type(allowed) is not bool:
            raise ValueError()
        upsell = payload.get('rate_limit_upsell')
        if upsell is None:
            upsell = {}
        if not isinstance(upsell, dict):
            raise ValueError()
        return CodexQuotaSnapshot(credential_id=credential_id, version=1, fetched_at=received_at,
            plan_type=payload.get('plan_type'), primary=_window(limits.get('primary_window'), received_at),
            secondary=_window(limits.get('secondary_window'), received_at), limit_reached=limits.get('limit_reached'),
            allowed=allowed, banner_type=upsell.get('banner_type'),
            additional_rate_limits=_additional_limits(payload, received_at))
    except (ValueError, TypeError, OverflowError, ValidationError):
        raise GatewayError('codex_usage_unavailable', 502, 'quota') from None


def parse_credits(payload, credential_id, received_at):
    try:
        if not isinstance(payload, dict):
            raise ValueError()
        if 'data' in payload:
            if any(k in payload for k in ('credits', 'available_count', 'availableCount')):
                raise ValueError()
            payload = payload['data']
        if not isinstance(payload, dict):
            raise ValueError()
        count = _alias(payload, 'available_count', 'availableCount')
        if count is not None:
            count = _integer(count, 1000000)
        records = payload.get('credits', [])
        if not isinstance(records, list) or len(records) > 1000:
            raise ValueError()
        result, seen = [], set()
        for row in records:
            if not isinstance(row, dict):
                raise ValueError()
            identity = _alias(row, 'id', 'credit_id', 'creditId')
            if identity is not None:
                if not isinstance(identity, str) or identity in seen:
                    raise ValueError()
                seen.add(identity)
            state = _alias(row, 'status', 'state')
            if state is not None and not isinstance(state, str):
                raise ValueError()
            state = state if state in {'available', 'redeemed', 'expired'} else 'unknown'
            expiry = _alias(row, 'expires_at', 'expiresAt')
            result.append(ResetCredit(id=identity, state=state, expires_at=_timestamp(expiry) if expiry is not None else None))
        # An aggregate is authoritative unless complete detail proves a
        # contradiction. Empty/unknown/expiry-less detail cannot prove zero.
        classified = bool(result) and all(r.state != 'unknown' and r.expires_at is not None for r in result)
        if classified:
            derived = sum(r.state == 'available' and r.expires_at > received_at for r in result)
            if count is not None and count != derived:
                raise ValueError()
            if count is None:
                count = derived
        return ResetCreditsSnapshot(credential_id=credential_id, version=1, fetched_at=received_at,
                                    available_count=count, credits=tuple(result))
    except (ValueError, TypeError, OverflowError, ValidationError):
        raise GatewayError('codex_credits_unavailable', 502, 'quota') from None


class QuotaClient:
    def __init__(self, transport):
        self.transport = transport

    async def _get(self, suffix, route, lease, tokens, account_id, deadline):
        if route.root != ACCOUNT_ROOT:
            raise GatewayError('egress_denied', 502, 'upstream')
        try:
            request = OutboundRequest('GET', suffix, None, kind='codex_account',
                auth_header=('Authorization', 'Bearer ' + tokens['access_token']),
                headers=(('chatgpt-account-id', account_id),),
                deadline=time.monotonic() + max(0, (deadline-datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, KeyError, TypeError):
            raise GatewayError('invalid_request') from None
        async with self.transport.open(route, lease.proxy, request) as response:
            if response.status != 200:
                raise UpstreamRejected(response.status)
            result = await response.read_json(max_bytes=2097152)
            if not isinstance(result, dict):
                raise GatewayError('upstream_error', 502, 'upstream')
            return result

    async def usage(self, route, lease, tokens, account_id, deadline):
        return await self._get('usage', route, lease, tokens, account_id, deadline)

    async def credits(self, route, lease, tokens, account_id, deadline):
        return await self._get('rate-limit-reset-credits', route, lease, tokens, account_id, deadline)

    @asynccontextmanager
    async def consume(self, route, lease, tokens, account_id, deadline, redeem_request_id):
        """Yield safe receipt headers before close; never parse a redemption body."""
        if route.root != ACCOUNT_ROOT:
            raise GatewayError('egress_denied', 502, 'upstream')
        try:
            request = OutboundRequest('POST', 'rate-limit-reset-credits/consume',
                {'redeem_request_id': str(redeem_request_id)}, kind='codex_account',
                auth_header=('Authorization', 'Bearer ' + tokens['access_token']),
                headers=(('chatgpt-account-id', account_id),),
                deadline=time.monotonic() + max(0, (deadline-datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, KeyError, TypeError):
            raise GatewayError('invalid_request') from None
        async with self.transport.open(route, lease.proxy, request) as response:
            now = datetime.now(timezone.utc)
            yield ResetReceipt(response.status,
                parse_retry_after(response.headers.get('retry-after'), now=now) if response.status == 429 else None, now)
