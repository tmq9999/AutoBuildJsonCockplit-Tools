"""One bounded, explicitly authorized inference attempt; no customer accounting."""

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import time

from sqlalchemy import text

from ..contracts import GenerationOptions, InferenceRequest, Message, Text
from ..errors import GatewayError, UpstreamRejected
from ..metering.records import Bounds
from ..protocols.common import EventCollector
from ..providers.preflight import validate_request
from ..proxy.config import ProxySelection
from ..routing.records import RouteSnapshot
from .operations import _json
from .probe_quote import OUTPUT_BOUND, PROMPT
from .records import OperationRequest


class ProbeService:
    def __init__(self, db, sources, ops, quotes, accounting, adapters, transport,
                 limits, proxies, profiles, catalog):
        self.db, self.sources, self.ops = db, sources, ops
        self.quotes, self.accounting = quotes, accounting
        self.adapters, self.transport, self.limits = adapters, transport, limits
        self.proxies, self.profiles, self.catalog = proxies, profiles, catalog

    async def recover(self, operation_id):
        return await self.accounting.settle(operation_id)

    async def execute(self, claim):
        remaining = (claim.deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return await self.ops.get(claim.operation_id)
        owner = asyncio.create_task(self._owned(claim))
        try:
            return await asyncio.wait_for(asyncio.shield(owner), remaining)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            owner.cancel()
            cancelled = isinstance(exc, asyncio.CancelledError)
            cleanup = asyncio.create_task(asyncio.wait_for(self._join(owner, claim), 5))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            try:
                result = await cleanup
            finally:
                if cancelled:
                    raise asyncio.CancelledError
            return result

    async def _join(self, owner, claim):
        await asyncio.gather(owner, return_exceptions=True)
        return await self.ops.get(claim.operation_id)

    async def _route(self, claim):
        async with self.db.sessions.begin() as session:
            context = await self.sources.lock_context(session, claim.source_id)
            await self.ops.assert_claim(session, claim)
            row = await self.ops._row(session, claim.operation_id)
            command = OperationRequest.model_validate_json(_json({**row["input"], "id": str(row["id"])}))
            quote = await self.quotes.quote_in(session, context, command.probe.binding_id)
            if quote.stamp != command.probe.expected or quote.digest != command.probe.quote_digest:
                raise GatewayError("version_conflict", 409)
            binding = (await session.execute(text("SELECT * FROM model_bindings WHERE id=:id"),
                                              {"id": command.probe.binding_id})).mappings().one()
            config, provider = binding["config"], context.provider
            route = RouteSnapshot(binding["id"], context.source.provider_id, context.source.credential_id,
                binding["model_id"], config["upstream_model"], provider.adapter, provider.root,
                provider.auth_mode, context.stamp.provider_version, frozenset(config["capabilities"]),
                Bounds(quote.input_bound, OUTPUT_BOUND), context.effective_proxy_id, provider.budget_id,
                config["priority"], 0, 0, min(provider.timeout, 60), provider.cost_schedule, provider.wire_api)
            return context, quote, route

    async def _current(self, claim, expected):
        await self.ops.read_claim(claim)
        current = await self.quotes.quote(claim.source_id, expected.stamp.binding_id)
        if current != expected:
            raise GatewayError("version_conflict", 409)

    async def _owned(self, claim):
        resources = _Resources()
        pulse = None
        failure = None
        context = quote = route = None
        owner = asyncio.current_task()

        async def heartbeat():
            nonlocal failure
            try:
                while True:
                    await asyncio.sleep(1)
                    await self._current(claim, quote)
                    if not await self.ops.heartbeat(claim):
                        raise GatewayError("claim_lost", 409)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = exc
                owner.cancel()

        try:
            context, quote, route = await self._route(claim)
            adapter = self.adapters.get(route.adapter)
            if adapter is None or route.adapter == "codex_oauth":
                raise GatewayError("unsupported_feature")
            request = InferenceRequest(model=route.public_model_id, stream=True,
                messages=(Message(role="user", blocks=(Text(text=PROMPT),)),),
                options=GenerationOptions(max_output_tokens=OUTPUT_BOUND))
            validate_request(request, route)
            pulse = asyncio.create_task(heartbeat())
            selection = (ProxySelection("direct") if context.effective_proxy_id is None
                         else await self.profiles.load(context.effective_proxy_id))
            lease = await resources.stack.enter_async_context(self.proxies.acquire(selection, claim.owner, claim.deadline))
            await self.transport._validate(route, lease.proxy)
            await resources.stack.enter_async_context(self.limits.acquire(route, claim.deadline))
            credential = await adapter.credential_resolver(route)
            await self._current(claim, quote)
            await self.accounting.reserve(claim, quote)
            await self._current(claim, quote)
            await self.accounting.mark_dispatched(claim)
            try:
                async def on_rejected(exc):
                    exc.received_at = time.monotonic()
                    await self.accounting.record_evidence(claim, None, "rejected",
                        status=exc.upstream_status, retry_after=exc.retry_after)
                stream = await resources.stack.enter_async_context(
                    adapter.open(request, route, lease, on_rejected=on_rejected, credential=credential))
                collector = EventCollector("probe", route.public_model_id)
                nonempty = False
                async for event in stream.events:
                    if (event.kind == "tool_delta" or event.block is not None and not isinstance(event.block, Text)
                            or event.finish_reason == "tool_calls"):
                        raise GatewayError("unsupported_feature", 502)
                    # Validate canonical event structure without keeping generated text.
                    if event.kind == "text_delta":
                        nonempty |= bool(event.delta.strip())
                        event = event.model_copy(update={"delta": ""})
                    elif event.block is not None:
                        nonempty |= bool(event.block.text.strip())
                        event = event.model_copy(update={"block": Text(text="")})
                    collector.feed(event)
                if not collector.terminal or collector.failed:
                    raise GatewayError("upstream_error", 502)
                await self.accounting.record_evidence(claim, collector.usage,
                                                      "success" if nonempty else "uncertain")
            except UpstreamRejected as exc:
                if exc.upstream_status == 429:
                    await self.catalog.cooldown(route.provider_id, None,
                        exc.retry_after if exc.retry_after is not None else 60, started_at=exc.received_at)
                failure = exc
        except asyncio.CancelledError:
            if failure is None:
                failure = GatewayError("operation_cancelled", 409)
        except Exception as exc:
            failure = exc
        finally:
            if pulse is not None:
                pulse.cancel()
                await asyncio.gather(pulse, return_exceptions=True)
            await resources.close()
        try:
            if failure is not None and not isinstance(failure, GatewayError):
                raise GatewayError("storage_unavailable", 503, "storage") from None
            return await self.accounting.settle(claim.operation_id, claim=claim,
                                                error=failure.code if failure is not None else None)
        except GatewayError as exc:
            if exc.code in {"claim_lost", "operation_expired"}:
                return await self.ops.get(claim.operation_id)
            raise


class _Resources:
    """All I/O exits share one bounded, joined close task, even on cancellation."""

    def __init__(self):
        self.stack = AsyncExitStack()
        self._close_task = None

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(asyncio.wait_for(self.stack.aclose(), 5))
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                continue
        await self._close_task
