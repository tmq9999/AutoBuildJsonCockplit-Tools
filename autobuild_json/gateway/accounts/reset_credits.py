"""One explicit confirmation, one durable redemption UUID, at most one POST."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ..errors import GatewayError
from .quota_client import ACCOUNT_ROOT

DEFINITIVE_REJECTIONS = frozenset({400, 401, 403, 404, 409, 422, 429})


class ResetCreditService:
    def __init__(self, quota):
        self.quota, self.store = quota, quota.store

    async def consume(self, credential_id, request_id, credits_version, acknowledge, actor, deadline):
        now = datetime.now(timezone.utc)
        if not isinstance(deadline, datetime) or deadline.tzinfo is None or deadline.utcoffset() is None:
            raise GatewayError('invalid_request')
        deadline = min(deadline, now + timedelta(seconds=30))
        if deadline <= now:
            raise GatewayError('deadline_exceeded', 504, 'quota')
        progress = {'preparing': False}
        owner = asyncio.create_task(self._consume(credential_id, request_id, credits_version, acknowledge,
                                                 actor, deadline, progress))
        try:
            return await asyncio.wait_for(asyncio.shield(owner), (deadline-now).total_seconds())
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            owner.cancel()
            async def join():
                await asyncio.gather(owner, return_exceptions=True)
                if progress['preparing']:
                    await self.store.recover_expired()
                    return await self.store.get_reset(request_id)
                return None
            cleanup = asyncio.create_task(asyncio.wait_for(join(), 5))
            cancelled = isinstance(exc, asyncio.CancelledError)
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            try:
                result = await cleanup
            except asyncio.TimeoutError:
                if cancelled:
                    raise asyncio.CancelledError
                raise GatewayError('deadline_exceeded', 504, 'quota') from None
            if cancelled:
                raise asyncio.CancelledError
            if result is not None:
                return result
            raise GatewayError('deadline_exceeded', 504, 'quota') from None

    async def _consume(self, credential_id, request_id, credits_version, acknowledge, actor, deadline, progress):
        replay = await self.store.replay_reset(credential_id, request_id, credits_version, acknowledge)
        if replay is not None:
            return replay
        try:
            await self.store.preflight_reset(credential_id, credits_version)
        except GatewayError:
            replay = await self.store.replay_reset(credential_id, request_id, credits_version, acknowledge)
            if replay is not None:
                return replay
            raise
        tokens = await self.quota.credentials.fresh_tokens(credential_id, deadline)
        selection, policy = await self.quota.resolver.policy(credential_id)
        row = await self.quota.credentials._record(credential_id)
        if row['token_generation'] != policy['token_generation'] or self.quota.credentials._tokens(row) != tokens:
            raise GatewayError('claim_lost', 409, 'quota')
        progress['preparing'] = True
        claim = await self.store.prepare_reset(credential_id, request_id, credits_version, actor, deadline=deadline)
        if not claim.is_new:
            return claim.result
        _, current = await self.quota.resolver.policy(credential_id)
        if current != policy:
            raise GatewayError('claim_lost', 409, 'quota')
        route = SimpleNamespace(root=ACCOUNT_ROOT, proxy_profile_id=policy['proxy_id'])
        dispatched, receipt = False, None
        try:
            async with self.quota.proxies.acquire(selection, request_id, deadline) as lease:
                if not await self.store.mark_dispatched(request_id, claim.generation):
                    raise GatewayError('claim_lost', 409, 'quota')
                dispatched = True
                async with self.quota.client.consume(route, lease, tokens, row['account_id'], deadline,
                                                     claim.redeem_request_id) as receipt:
                    state = ('succeeded_refresh_failed' if 200 <= receipt.status < 300 else
                             'rejected' if receipt.status in DEFINITIVE_REJECTIONS else 'unknown')
                    code = ('codex_refresh_failed' if state == 'succeeded_refresh_failed' else
                            'rate_limited' if receipt.status == 429 else 'upstream_error' if state == 'rejected'
                            else 'codex_reset_uncertain')
                    # Receipt is durable before response close or proxy cleanup.
                    await self.store.finish_reset(request_id, claim.generation, state, code, receipt.status,
                        retry_after=receipt.retry_after, received_at=receipt.received_at)
        except (Exception, asyncio.CancelledError) as exc:
            if not dispatched:
                raise
            await self._uncertain(claim)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return await self.store.get_reset(request_id)
        if 200 <= receipt.status < 300:
            try:
                await self.quota.refresh(credential_id, deadline)
                # Public refresh may be partial: only the storage fence proves
                # BOTH observations share a complete post-receipt generation.
                return await self.store.finish_reset(request_id, claim.generation, 'succeeded', None, receipt.status)
            except GatewayError:
                pass
        return await self.store.get_reset(request_id)

    async def _uncertain(self, claim):
        try:
            await self.store.finish_reset(claim.result.operation_id, claim.generation,
                                         'unknown', 'codex_reset_uncertain', None)
        except GatewayError:
            # Expired dispatch uses recovery's generation fence. A durable
            # receipt or administrative resolution is never overwritten.
            await self.store.recover_expired()

    async def resolve(self, operation_id, version, outcome, reason, actor):
        return await self.store.resolve_reset(operation_id, version, outcome, reason, actor)
