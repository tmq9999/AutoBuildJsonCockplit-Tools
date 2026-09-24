"""Guarded account GETs and allowlisted, unknown-preserving parsers."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time

from pydantic import ValidationError

from ..errors import GatewayError, UpstreamRejected
from ..transport.http import OutboundRequest
from .quota_records import CodexQuotaSnapshot, QuotaWindow, ResetCredit, ResetCreditsSnapshot

ACCOUNT_ROOT = 'https://chatgpt.com/backend-api/wham'


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
        return CodexQuotaSnapshot(credential_id=credential_id, version=1, fetched_at=received_at,
            plan_type=payload.get('plan_type'), primary=_window(limits.get('primary_window'), received_at),
            secondary=_window(limits.get('secondary_window'), received_at), limit_reached=limits.get('limit_reached'))
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
