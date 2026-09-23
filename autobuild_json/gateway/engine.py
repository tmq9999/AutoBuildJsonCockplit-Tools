from contextlib import AsyncExitStack
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import hmac
import json
from uuid import UUID, uuid4

from sqlalchemy import text

from .errors import GatewayError, UpstreamRejected
from .metering.records import Admission, Bounds
from .metering.budgets import BudgetService
from .metering.costs import estimate_cost
from .protocols.common import EventCollector
from .providers.openai import OpenAIAdapter
from .proxy.config import ProxySelection
from .secrets import Ciphertext
from .routing.continuations import ContinuationStore, ContinuationScope, ContinuationBinding


@dataclass(frozen=True)
class RequestMeta:
    request_id: UUID
    admitted_at: datetime
    deadline: datetime
    idempotency_digest: bytes | None = None
    payload_digest: bytes | None = None
    protocol: str = "openai"


class Engine:
    def __init__(self, db, catalog, ledger, proxies, transport, vault, *, digest_key=None, proxy_resolver=None):
        self.db, self.catalog, self.ledger = db, catalog, ledger
        self.proxies, self.transport, self.vault = proxies, transport, vault
        self.digest_key = digest_key
        self.proxy_resolver = proxy_resolver
        self.budgets = BudgetService(db)
        self.continuations = ContinuationStore(db, vault)
        self.adapters = {"openai_compatible": OpenAIAdapter(transport, self.credential)}

    async def credential(self, route):
        async with self.db.sessions() as session:
            cipher = await session.scalar(text("SELECT encrypted_secret FROM credentials WHERE id=:id AND enabled "
                                              "AND provider_id=:provider"), {"id": route.credential_id, "provider": route.provider_id})
        if cipher is None:
            raise GatewayError("upstream_unavailable", 503)
        return self.vault.open("credential", route.credential_id, Ciphertext.from_dict(cipher)).decode()

    def meta(self, body, idempotency_key=None, *, protocol="openai"):
        now = datetime.now(timezone.utc)
        claim, payload = None, None
        if idempotency_key is not None:
            if not self.digest_key or not 1 <= len(idempotency_key) <= 200:
                raise GatewayError("invalid_request")
            claim = hmac.new(self.digest_key, b"abgw:claim:"+idempotency_key.encode(), hashlib.sha256).digest()
            payload = hmac.new(self.digest_key, b"abgw:payload:"+json.dumps(body, sort_keys=True,
                               separators=(",", ":")).encode(), hashlib.sha256).digest()
        return RequestMeta(uuid4(), now, now+timedelta(seconds=180), claim, payload, protocol)

    async def prepare(self, principal, request, meta):
        routes = await self.catalog.candidates(principal, request)
        scope = ContinuationScope(principal.customer_id, principal.key_id, routes[0].public_model_id)
        if request.continuation:
            binding = await self.continuations.resolve(request.continuation, scope)
            routes = [route for route in routes if route.binding_id == binding.route_id and route.credential_id == binding.credential_id]
            if not routes:
                raise GatewayError("invalid_state")
            request = request.model_copy(update={"continuation": binding.upstream_id})
        route = routes[0]
        if route.adapter not in self.adapters:
            raise GatewayError("upstream_unavailable", 503)
        max_output = request.options.max_output_tokens or min(4096, route.bounds.output_tokens)
        if max_output > route.bounds.output_tokens:
            raise GatewayError("invalid_request")
        request = request.model_copy(update={"options": request.options.model_copy(update={"max_output_tokens": max_output})})
        bounds = Bounds(route.bounds.input_tokens, max_output)
        deadline = min(meta.deadline, datetime.now(timezone.utc)+timedelta(seconds=route.timeout))
        hold = await self.ledger.reserve(Admission(meta.request_id, principal, route.public_model_id, bounds,
            route.input_micro, route.output_micro, deadline, meta.idempotency_digest, meta.payload_digest, meta.protocol))
        stack = AsyncExitStack()
        dispatched = False
        budget_attempt = None
        try:
            for number, selected in enumerate(routes[:2]):
                if selected.adapter not in self.adapters or max_output > selected.bounds.output_tokens:
                    raise GatewayError("upstream_unavailable", 503)
                if number:
                    hold = await self.ledger.resize(hold, Bounds(selected.bounds.input_tokens, max_output))
                selection = ProxySelection("direct")
                if selected.proxy_profile_id is not None:
                    if self.proxy_resolver is None:
                        raise GatewayError("proxy_not_ready", 503, "proxy")
                    selection = await self.proxy_resolver(selected.proxy_profile_id)
                lease = await stack.enter_async_context(self.proxies.acquire(selection, meta.request_id, deadline))
                attempt = uuid4()
                # Resolve credential before dispatch flag so bad local config has no
                # uncertain-charge side effect. Adapter repeats current enabled check.
                await self.credential(selected)
                if selected.budget_id is not None:
                    price = selected.cost_schedule
                    if price is None:
                        raise GatewayError("upstream_unavailable", 503)
                    rates = [price.input_per_million, price.cache_read_per_million or price.input_per_million,
                             price.cache_write_per_million or price.input_per_million]
                    upper = (selected.bounds.input_tokens*max(rates)+max_output*price.output_per_million)/1_000_000
                    await self.budgets.reserve(selected.budget_id, attempt, price.currency,
                                               upper.quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING))
                    budget_attempt = attempt
                await self.ledger.mark_dispatched(meta.request_id, attempt)
                dispatched = True
                try:
                    stream = await stack.enter_async_context(self.adapters[selected.adapter].open(request, selected, lease))
                except UpstreamRejected as exc:
                    await self.ledger.mark_rejected(meta.request_id, attempt, "rejected_before_generation")
                    dispatched = False
                    if budget_attempt is not None:
                        await self.budgets.settle(budget_attempt, Decimal(0))
                        budget_attempt = None
                    await stack.aclose()
                    stack = AsyncExitStack()
                    if exc.upstream_status == 429:
                        await self.catalog.cooldown(selected.provider_id, None, exc.retry_after or 60)
                    if not exc.safe_retry or number == 1 or len(routes) < 2 or routes[1].provider_id == selected.provider_id:
                        raise
                    continue
                return PreparedCall(self, stack, stream, meta.request_id, selected, attempt, budget_attempt, scope)
            raise GatewayError("upstream_unavailable", 503)
        except BaseException:
            try:
                if budget_attempt is not None:
                    await self.budgets.settle(budget_attempt, None if dispatched else Decimal(0))
                if dispatched:
                    await self.ledger.mark_pending(meta.request_id, "interrupted")
                else:
                    await self.ledger.release_unspent(meta.request_id, "not_dispatched")
            finally:
                await stack.aclose()
            raise


class PreparedCall:
    def __init__(self, engine, stack, stream, request_id, route, attempt_id, budget_attempt=None, scope=None):
        self.engine, self.stack, self.stream = engine, stack, stream
        self.request_id, self.route, self.attempt_id = request_id, route, attempt_id
        self.budget_attempt = budget_attempt
        self.scope = scope
        self.collector = EventCollector("chatcmpl-"+request_id.hex, route.public_model_id)
        self.closed = self.settled = self.started = False

    async def _save_usage(self, usage):
        cost = estimate_cost(usage, self.route.cost_schedule)
        if self.budget_attempt is not None:
            await self.engine.budgets.settle(self.budget_attempt, cost.amount.quantize(Decimal("0.000000000001")) if cost else None)
        async with self.engine.db.sessions.begin() as session:
            await session.execute(text("UPDATE attempts SET usage=CAST(:usage AS jsonb),cost=CAST(:cost AS jsonb),status='completed' WHERE id=:id"),
                                  {"usage": json.dumps(asdict(usage)), "id": self.attempt_id,
                                   "cost": json.dumps({"amount": str(cost.amount), "currency": cost.currency, "source": cost.source}) if cost else None})

    async def events(self):
        if self.started:
            raise GatewayError("invalid_state")
        self.started = True
        try:
            async for event in self.stream.events:
                if event.kind == "started" and event.response_id is not None:
                    handle = self.collector.id
                    if "continuation" in self.route.capabilities:
                        handle = await self.engine.continuations.issue(self.scope, ContinuationBinding(
                            self.route.binding_id, self.route.credential_id, event.response_id,
                            datetime.now(timezone.utc)+timedelta(hours=24)))
                    self.collector.id = handle
                    event = event.model_copy(update={"response_id": handle})
                self.collector.feed(event)
                if event.kind == "finished":
                    usage = self.collector.usage
                    if usage is None:
                        await self.engine.ledger.mark_pending(self.request_id, "usage_missing")
                        raise GatewayError("usage_pending", 502, "stream")
                    await self._save_usage(usage)
                    await self.engine.ledger.settle(self.request_id, usage)
                    self.settled = True
                    yield event.model_copy(update={"usage": usage})
                else:
                    yield event
            if not self.settled:
                raise GatewayError("upstream_error", 502, "stream")
        except ValueError:
            raise GatewayError("upstream_error", 502, "stream") from None
        finally:
            await self.close()

    async def collect(self):
        async for _ in self.events():
            pass
        return self.collector.result()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if not self.settled:
                if self.budget_attempt is not None:
                    await self.engine.budgets.settle(self.budget_attempt, None)
                await self.engine.ledger.mark_pending(self.request_id, "interrupted")
        finally:
            try:
                await self.stream.cancel()
            finally:
                await self.stack.aclose()
