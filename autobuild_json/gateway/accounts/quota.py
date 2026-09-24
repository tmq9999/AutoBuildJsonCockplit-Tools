"""Explicit quota refresh; projections never allocate proxies or call upstream."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
from sqlalchemy import text

from ..errors import GatewayError
from .proxy_selection import AccountProxyResolver
from .quota_client import ACCOUNT_ROOT, QuotaClient, parse_usage, parse_credits


class CodexQuotaService:
    def __init__(self, db, credentials, proxies, profiles, transport, store):
        self.db, self.credentials, self.proxies, self.store = db, credentials, proxies, store
        self.resolver, self.client = AccountProxyResolver(db, profiles), QuotaClient(transport)

    async def get(self, credential_id):
        return await self.store.read(credential_id)

    async def get_credits(self, credential_id):
        return await self.store.read_credits(credential_id)

    async def refresh(self, credential_id, deadline):
        now = datetime.now(timezone.utc)
        if not isinstance(deadline, datetime) or deadline.tzinfo is None or deadline.utcoffset() is None:
            raise GatewayError('invalid_request')
        deadline = min(deadline, now + timedelta(seconds=30))
        if deadline <= now:
            raise GatewayError('deadline_exceeded', 504, 'quota')
        owner = asyncio.create_task(self._refresh(credential_id, deadline))
        try:
            return await asyncio.wait_for(asyncio.shield(owner), max(0, (deadline-now).total_seconds()))
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            owner.cancel()
            cleanup = asyncio.create_task(asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), 5))
            cancelled = isinstance(exc, asyncio.CancelledError)
            try:
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                await cleanup
            except asyncio.TimeoutError:
                if cancelled:
                    raise asyncio.CancelledError
                raise GatewayError('deadline_exceeded', 504, 'quota') from None
            if cancelled:
                raise asyncio.CancelledError
            raise GatewayError('deadline_exceeded', 504, 'quota') from None
        except GatewayError as exc:
            if exc.code == 'proxy_error' and datetime.now(timezone.utc) >= deadline:
                raise GatewayError('deadline_exceeded', 504, 'quota') from None
            raise

    async def _refresh(self, credential_id, deadline):
        selection, policy = await self.resolver.policy(credential_id)
        for attempt in range(2):
            # OAuth refresh owns a separate lease, never nested inside this operation.
            tokens = await self.credentials.fresh_tokens(credential_id, deadline)
            claim = await self.store.begin_refresh(credential_id, deadline)
            expected = claim.model_dump(mode='json', exclude={'credential_id', 'generation', 'deadline', 'token_generation'})
            if expected != {k: v for k, v in policy.items() if k != 'token_generation'}:
                raise GatewayError('claim_lost', 409, 'quota')
            row = await self.credentials._record(credential_id)
            if row['token_generation'] != claim.token_generation or self.credentials._tokens(row) != tokens:
                raise GatewayError('claim_lost', 409, 'quota')
            route = SimpleNamespace(root=ACCOUNT_ROOT, proxy_profile_id=claim.proxy_id)
            usage = credits = None
            errors = []
            retry = False
            async with self.proxies.acquire(selection, uuid4(), deadline) as lease:
                for kind, parser in (('usage', parse_usage), ('credits', parse_credits)):
                    if not await self.store.assert_refresh(claim):
                        raise GatewayError('claim_lost', 409, 'quota')
                    try:
                        payload = await getattr(self.client, kind)(route, lease, tokens, row['account_id'], deadline)
                        value = parser(payload, credential_id, datetime.now(timezone.utc))
                        if kind == 'usage':
                            usage = value
                        else:
                            credits = value
                    except GatewayError as exc:
                        if getattr(exc, 'upstream_status', None) == 401 and attempt == 0:
                            retry = True
                            break
                        errors.append('codex_' + kind + '_unavailable')
            if retry:
                # Save any successful subfetch with its original observation
                # stamp before OAuth changes token_generation or health.
                if not await self.store.save_refresh(claim, usage, credits,
                        ('codex_usage_unavailable', 'codex_credits_unavailable')):
                    raise GatewayError('claim_lost', 409, 'quota')
                async with self.db.sessions.begin() as session:
                    changed = await session.scalar(text("UPDATE credentials SET token_expires_at=now()-interval '1 second' "
                        "WHERE id=:id AND token_generation=:generation AND health='active' RETURNING id"),
                        {'id': credential_id, 'generation': claim.token_generation})
                if changed:
                    continue
                raise GatewayError('claim_lost', 409, 'quota')
            if not await self.store.save_refresh(claim, usage, credits, errors):
                raise GatewayError('claim_lost', 409, 'quota')
            result = await self.get(credential_id)
            if result is None:
                raise GatewayError('codex_usage_unavailable', 502, 'quota')
            return result
        raise GatewayError('upstream_error', 502, 'quota')
