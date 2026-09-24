"""Fenced, deadline-bounded execution of provider catalog operations."""

import asyncio
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

from ..errors import GatewayError, UpstreamRejected
from ..proxy.config import ProxySelection


class OperationRunner:
    def __init__(self, sources, ops, snapshots, discovery, proxies, profiles, catalog, probe=None):
        self.sources, self.ops, self.snapshots = sources, ops, snapshots
        self.discovery, self.proxies, self.profiles = discovery, proxies, profiles
        self.catalog, self.probe = catalog, probe

    async def run(self, operation_id: UUID):
        current = await self.ops.get(operation_id)
        if current.state != "queued":
            return current
        claim = await self.ops.claim(current.id, uuid4())
        return await self.execute(claim) if claim is not None else await self.ops.get(current.id)

    async def _durable(self, awaitable):
        """Never rewrite a failed durable transaction with another terminal result."""
        try:
            return await awaitable
        except GatewayError as exc:
            if exc.code in {"claim_lost", "operation_expired", "operation_cancelled",
                            "catalog_snapshot_stale", "version_conflict", "invalid_state"}:
                raise
            raise GatewayError("storage_unavailable", 503, "storage") from None
        except Exception:
            raise GatewayError("storage_unavailable", 503, "storage") from None

    async def _current(self, claim):
        return await self._durable(self.ops.get(claim.operation_id))

    async def _finish(self, claim, state, *, result=None, error=None, context=None):
        try:
            return await self._durable(self.ops.finish(claim, state, result=result, error=error, context=context))
        except GatewayError as exc:
            if exc.code in {"claim_lost", "operation_expired"}:
                return await self._current(claim)
            raise

    async def execute(self, claim):
        operation = await self.ops.get(claim.operation_id)
        if operation.kind == "probe" and self.probe is not None:
            return await self.probe.execute(claim)
        remaining = (claim.deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return await asyncio.wait_for(self._current(claim), 5)
        # The one owner acquires, uses and exits the proxy context itself.
        owner = asyncio.create_task(self._owned(claim))
        try:
            return await asyncio.wait_for(asyncio.shield(owner), remaining)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            owner.cancel()
            cancelled = isinstance(exc, asyncio.CancelledError)
            async def finalize():
                await asyncio.gather(owner, return_exceptions=True)
                if cancelled:
                    return await self._finish(claim, "cancelled", error="operation_cancelled")
                return await self._current(claim)
            # One shared budget covers resource cleanup AND durable finalization.
            cleanup = asyncio.create_task(asyncio.wait_for(finalize(), 5))
            # Repeated caller cancellation must not detach the cleanup task.
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
                    continue
                except asyncio.TimeoutError:
                    break
            try:
                result = await cleanup
            except asyncio.TimeoutError:
                raise GatewayError("storage_unavailable", 503, "storage") from None
            finally:
                if cancelled:
                    raise asyncio.CancelledError
            return result

    async def _owned(self, claim):
        owner = asyncio.current_task()
        pulse = None
        interruption = None
        context = None

        async def check_current():
            await self.ops.read_claim(claim)
            try:
                current = await self.sources.context(claim.source_id)
            except GatewayError as exc:
                if exc.code in {"not_found", "invalid_state", "invalid_request"}:
                    raise GatewayError("catalog_snapshot_stale", 409) from None
                raise
            if not self.ops._same_source_stamp(current.stamp, context.stamp):
                raise GatewayError("catalog_snapshot_stale", 409)

        async def heartbeat():
            nonlocal interruption
            try:
                while True:
                    await asyncio.sleep(1)
                    await check_current()
                    if not await self.ops.heartbeat(claim):
                        # heartbeat() deliberately returns only whether the
                        # fenced UPDATE matched.  Re-read the claim to
                        # distinguish an expected cancellation from genuine
                        # owner/generation loss; otherwise a cancel racing
                        # this update would leave the row running forever.
                        try:
                            await self.ops.read_claim(claim)
                        except GatewayError as exc:
                            if exc.code == "operation_cancelled":
                                raise
                            if exc.code in {"claim_lost", "operation_expired"}:
                                raise GatewayError("claim_lost", 409) from None
                            raise
                        raise GatewayError("claim_lost", 409)
            except asyncio.CancelledError:
                raise
            except GatewayError as exc:
                interruption = exc
                owner.cancel()
            except Exception:
                interruption = GatewayError("storage_unavailable", 503, "storage")
                owner.cancel()

        try:
            operation = await self.ops.read_claim(claim)
            expected = await self.ops.expected_stamp(claim)
            try:
                context = await self.sources.context(claim.source_id)
            except GatewayError as exc:
                if exc.code in {"not_found", "invalid_state", "invalid_request"}:
                    raise GatewayError("catalog_snapshot_stale", 409) from None
                raise
            if not self.ops._same_source_stamp(context.stamp, expected):
                raise GatewayError("catalog_snapshot_stale", 409)
            pulse = asyncio.create_task(heartbeat())
            if operation.kind == "probe" and self.probe is None:
                raise GatewayError("unsupported_feature")
            selection = (ProxySelection("direct") if context.effective_proxy_id is None
                         else await self.profiles.load(context.effective_proxy_id))
            warnings = set()
            entries, result = None, None
            deadline = time.monotonic() + max(0, (claim.deadline - datetime.now(timezone.utc)).total_seconds())
            async with self.proxies.acquire(selection, claim.owner, claim.deadline) as lease:
                await check_current()
                await self.ops.mark_dispatched(claim, uuid4())
                if operation.kind == "discover":
                    entries = await self.discovery.collect(context, lease, check_current, deadline,
                                                           on_warnings=warnings.update)
                elif operation.kind == "check":
                    result = await self.discovery.check(context, lease, check_current, deadline)
            # Do not let our heartbeat cancel a just-committed terminal result.
            # Atomic persistence fences below replace it after HTTP cleanup.
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            pulse = None
            await check_current()
            if entries is not None:
                await self._durable(self.snapshots.commit(claim, context, entries, warnings=tuple(sorted(warnings))))
                return await self._current(claim)
            return await self._finish(claim, "succeeded", result=result, context=context)
        except asyncio.CancelledError:
            if interruption is None:
                raise
            return await self._failure(claim, interruption, context)
        except GatewayError as exc:
            return await self._failure(claim, exc, context)
        except Exception:
            return await self._failure(claim, GatewayError("internal_error", 500), context)
        finally:
            if pulse is not None:
                pulse.cancel()
                await asyncio.gather(pulse, return_exceptions=True)

    async def _failure(self, claim, exc, context):
        if exc.code == "storage_unavailable":
            raise exc
        if exc.code in {"claim_lost", "operation_expired", "deadline_exceeded"}:
            return await self._current(claim)
        if isinstance(exc, UpstreamRejected) and exc.upstream_status == 429:
            await self._durable(self.catalog.cooldown(context.route.provider_id, None,
                exc.retry_after if exc.retry_after is not None else 60,
                started_at=getattr(exc, "received_at", time.monotonic())))
        state = ("cancelled" if exc.code == "operation_cancelled" else
                 "stale" if exc.code in {"catalog_snapshot_stale", "version_conflict", "invalid_state"} else "failed")
        return await self._finish(claim, state, error=exc)
