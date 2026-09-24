"""Fenced execution of provider catalog operations."""

import asyncio
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

from ..errors import GatewayError, UpstreamRejected
from ..proxy.config import ProxySelection


_WARNING_CODES = frozenset({"authentication_unverified", "metadata_incomplete", "catalog_partial",
                            "malformed_optional_metadata"})


class OperationRunner:
    """Run one claimed operation while ownership and resources remain fenced."""

    def __init__(self, sources, ops, snapshots, discovery, proxies, profiles, catalog, probe=None):
        self.sources = sources
        self.ops = ops
        self.snapshots = snapshots
        self.discovery = discovery
        self.proxies = proxies
        self.profiles = profiles
        self.catalog = catalog
        self.probe = probe

    async def run(self, operation_id: UUID):
        current = await self.ops.get(operation_id)
        if current.state != "queued":
            return current
        claim = await self.ops.claim(operation_id, uuid4())
        if claim is None:
            return await self.ops.get(operation_id)
        return await self.execute(claim)

    async def _terminal(self, claim, state, *, error=None, result=None, fenced=False):
        try:
            method = self.ops.finish_fenced if fenced else self.ops.finish
            return await method(claim, state, result=result, error=error)
        except GatewayError as exc:
            if exc.code == "claim_lost":
                return await self.ops.get(claim.operation_id)
            raise

    async def execute(self, claim):
        parent = asyncio.current_task()
        lost = False
        pulse = None

        async def heartbeat():
            nonlocal lost
            try:
                while True:
                    await asyncio.sleep(1)
                    if not await self.ops.heartbeat(claim):
                        state = await self.ops.get(claim.operation_id)
                        lost = not state.cancel_requested
                        parent.cancel()
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                lost = True
                parent.cancel()

        try:
            # Read the source once under the claim, then re-read it at each
            # dispatch boundary.  No database transaction survives into HTTP.
            await self.ops.read_claim(claim)
            try:
                expected = await self.ops.expected_stamp(claim)
                context = await self.sources.context(claim.source_id)
            except GatewayError:
                return await self._terminal(claim, "stale", error="version_conflict", fenced=True)
            if context.stamp != expected:
                return await self._terminal(claim, "stale", error="version_conflict", fenced=True)

            async def check_current():
                await self.ops.read_claim(claim)
                try:
                    current_context = await self.sources.context(claim.source_id)
                except GatewayError as exc:
                    raise GatewayError("catalog_snapshot_stale", 409) from exc
                if current_context.stamp != context.stamp:
                    raise GatewayError("catalog_snapshot_stale", 409)

            await check_current()
            if claim.deadline <= datetime.now(timezone.utc):
                return await self.ops.get(claim.operation_id)
            operation = await self.ops.read_claim(claim)
            if operation.kind == "probe" and self.probe is None:
                return await self._terminal(claim, "failed", error="unsupported_feature", fenced=True)
            if context.effective_proxy_id is None:
                selection = ProxySelection("direct")
            else:
                await check_current()
                selection = await self.profiles.load(context.effective_proxy_id)

            warnings = set()
            started_at = time.monotonic()
            pulse = asyncio.create_task(heartbeat())
            discovered_entries = None
            discovered_warnings = ()
            operation_result = None
            async with self.proxies.acquire(selection, claim.owner, claim.deadline) as lease:
                await self.ops.mark_dispatched(claim, uuid4())
                if operation.kind == "probe":
                    operation_result = await self.probe(context, lease, check_current, claim.deadline)
                elif operation.kind == "check":
                    operation_result = await self.discovery.check(context, lease, check_current,
                                                        time.monotonic() + max(0, (claim.deadline - datetime.now(timezone.utc)).total_seconds()))
                    warnings.update(w for w in operation_result.get("warnings", ()) if w in _WARNING_CODES)
                else:
                    def on_warnings(values):
                        warnings.update(w for w in values if w in _WARNING_CODES)

                    discovered_entries = await self.discovery.collect(
                        context, lease, check_current,
                        time.monotonic() + max(0, (claim.deadline - datetime.now(timezone.utc)).total_seconds()),
                        on_warnings=on_warnings,
                    )
                    discovered_warnings = tuple(sorted(warnings))
            # Lease/admission cleanup has completed before any durable success.
            if discovered_entries is not None:
                await check_current()
                await self.snapshots.commit(claim, context, discovered_entries, warnings=discovered_warnings)
                return await self.ops.get(claim.operation_id)
            if operation_result is not None:
                if warnings:
                    operation_result["warnings"] = sorted(warnings)
                return await self._terminal(claim, "succeeded", result=operation_result)
        except asyncio.CancelledError:
            if lost:
                return await self.ops.get(claim.operation_id)
            return await self._terminal(claim, "cancelled", error="operation_cancelled", fenced=True)
        except GatewayError as exc:
            if exc.code in {"catalog_snapshot_stale", "version_conflict", "invalid_state"}:
                return await self._terminal(claim, "stale", error="version_conflict", fenced=True)
            if exc.code == "operation_cancelled":
                return await self._terminal(claim, "cancelled", error=exc, fenced=True)
            if exc.code == "deadline_exceeded" or datetime.now(timezone.utc) >= claim.deadline:
                return await self.ops.get(claim.operation_id)
            if isinstance(exc, UpstreamRejected) and exc.retry_after is not None:
                try:
                    await self.catalog.cooldown(context.route.provider_id, None,
                                                exc.retry_after, started_at=started_at)
                except Exception:
                    pass
            return await self._terminal(claim, "failed", error=exc)
        except Exception:
            return await self._terminal(claim, "failed", error="internal_error")
        finally:
            if pulse is not None:
                pulse.cancel()
                await asyncio.gather(pulse, return_exceptions=True)
