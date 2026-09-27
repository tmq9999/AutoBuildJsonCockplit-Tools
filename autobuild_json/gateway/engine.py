import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import hmac
import inspect
import json
import time
from uuid import UUID, uuid4

from sqlalchemy import text

from .errors import GatewayError, UpstreamRejected
from .metering.records import Admission, Bounds, Usage
from .metering.budgets import BudgetService
from .metering.costs import estimate_cost
from .protocols.common import EventCollector
from .providers.openai import OpenAIAdapter
from .providers.anthropic import AnthropicAdapter
from .providers.gemini import GeminiAdapter
from .providers.ollama import OllamaAdapter
from .providers.codex import CodexAdapter
from .providers.preflight import validate_request
from .providers.limits import ProviderLimits
from .accounts.imports import CredentialService
from .accounts.proxy_selection import AccountProxyResolver
from .proxy.config import ProxySelection
from .secrets import Ciphertext
from .routing.continuations import ContinuationStore, ContinuationBinding
from .routing import session as routing_session
from .admission import InferenceAdmission
from .proxy.manager import ProxyCapacityError


@dataclass(frozen=True)
class RequestMeta:
    request_id: UUID
    admitted_at: datetime
    deadline: datetime
    idempotency_digest: bytes | None = None
    payload_digest: bytes | None = None
    protocol: str = "openai"
    session_digest: bytes | None = None


class Engine:
    def __init__(self, db, catalog, ledger, proxies, transport, vault, *, digest_key=None, proxy_resolver=None,
                 admission=None):
        self.db, self.catalog, self.ledger = db, catalog, ledger
        self.proxies, self.transport, self.vault = proxies, transport, vault
        self.digest_key = digest_key
        self.proxy_resolver = proxy_resolver
        self.admission = admission if admission is not None else InferenceAdmission(100)
        self.budgets = BudgetService(db)
        self.provider_limits=ProviderLimits(db)
        self.continuations = ContinuationStore(db, vault)
        self.credentials = CredentialService(db, vault, proxies, transport, profile_resolver=proxy_resolver)
        self.adapters = {"openai_compatible": OpenAIAdapter(transport, self.credential),
                         "anthropic": AnthropicAdapter(transport, self.credential), "gemini": GeminiAdapter(transport, self.credential),
                         "ollama": OllamaAdapter(transport, self.credential), "codex_oauth": CodexAdapter(transport, self.codex_tokens)}
        # Serving telemetry is owned by the app lifecycle. Request paths only
        # mark a coalesced update; they never spawn detached publisher tasks.
        self._capacity_task = None
        self._capacity_event = None
        self._capacity_dirty = False
        self._capacity_stopping = False
        self._capacity_interval = 0.05
        self._capacity_heartbeat = 10.0
        # Prepared calls remain owned by the engine after a request task is
        # cancelled or its bounded close caller times out.  This set is the
        # lifecycle drain path; it is deliberately not a detached fire-and-
        # forget cleanup queue.
        self._prepared_cleanup_tasks = set()

    async def drain_prepared_cleanup(self, timeout=5.0):
        """Join all retained request cleanup supervisors during shutdown."""
        registry = getattr(self, "_prepared_cleanup_tasks", set())
        for task in tuple(registry):
            if task.done():
                registry.discard(task)
        tasks = tuple(registry)
        if not tasks:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*(asyncio.shield(task) for task in tasks)),
                                   timeout=max(0.01, timeout))
        except asyncio.TimeoutError:
            # Keep ownership and let a later lifecycle drain retry; no child
            # is cancelled here because cancellation-resistant dependencies
            # must retain route/provider/proxy ownership until they unwind.
            raise TimeoutError("prepared cleanup did not drain") from None

    def capacity_snapshot(self):
        proxy = self.proxies.snapshot() if hasattr(self.proxies, "snapshot") else {}
        return {"admission": self.admission.snapshot(),
                "provider": self.provider_limits.snapshot(),
                "proxy": proxy}

    async def _publish_capacity(self):
        """Publish redacted serving-process telemetry for the admin process."""
        if self.db is None or not hasattr(self.db, "sessions"):
            return
        try:
            async with self.db.sessions.begin() as session:
                await session.execute(text(
                    "INSERT INTO gateway_capacity_snapshots(id,snapshot,updated_at) "
                    "VALUES (1,CAST(:snapshot AS jsonb),clock_timestamp()) "
                    "ON CONFLICT (id) DO UPDATE SET snapshot=EXCLUDED.snapshot,updated_at=EXCLUDED.updated_at"),
                    {"snapshot": json.dumps(self.capacity_snapshot(), separators=(",", ":"))})
        except Exception:
            # Telemetry must never turn a successful inference into a 5xx.
            return

    async def _capacity_publisher(self):
        """Coalescing, single-writer capacity publisher."""
        event = self._capacity_event
        while event is not None:
            try:
                await asyncio.wait_for(event.wait(), getattr(self, "_capacity_heartbeat", 10.0))
                event.clear()
            except asyncio.TimeoutError:
                # Keep a healthy idle serving process fresh for admin readers;
                # this remains below the 30-second stale threshold.
                self._capacity_dirty = True
            if self._capacity_interval:
                await asyncio.sleep(self._capacity_interval)
            if not self._capacity_dirty and self._capacity_stopping:
                break
            if not self._capacity_dirty:
                # An Event may remain set after a burst was consumed. Do not
                # turn that stale signal into an extra database write.
                continue
            self._capacity_dirty = False
            await self._publish_capacity()
            if self._capacity_stopping and not self._capacity_dirty:
                break

    async def start_capacity_publisher(self):
        """Start the lifecycle-owned publisher, idempotently."""
        task = getattr(self, "_capacity_task", None)
        if task is not None and not task.done():
            return
        self._capacity_event = asyncio.Event()
        self._capacity_stopping = False
        self._capacity_dirty = True
        task = asyncio.create_task(self._capacity_publisher(), name="gateway-capacity-publisher")
        self._capacity_task = task

        def clear_finished(done):
            if getattr(self, "_capacity_task", None) is done:
                self._capacity_task = None

        task.add_done_callback(clear_finished)
        self._capacity_event.set()

    def request_capacity_publish(self):
        """Request a coalesced update without creating a task."""
        self._capacity_dirty = True
        event = getattr(self, "_capacity_event", None)
        if event is not None:
            event.set()

    def _begin_resource_wait(self, resource):
        admission = getattr(self, "admission", None)
        begin = getattr(admission, "begin_resource_wait", None)
        if begin is None:
            return None
        episode = begin(resource)
        publish = getattr(self, "request_capacity_publish", None)
        if publish is not None:
            publish()
        return episode

    def _end_resource_wait(self, episode, outcome="success"):
        if episode is None:
            return
        admission = getattr(self, "admission", None)
        end = getattr(admission, "end_resource_wait", None)
        if end is not None:
            end(episode, outcome)
        publish = getattr(self, "request_capacity_publish", None)
        if publish is not None:
            publish()

    def _record_stage_outcome(self, stage, outcome):
        admission = getattr(self, "admission", None)
        record = getattr(admission, "record_stage_outcome", None)
        if record is not None:
            record(stage, outcome)
        publish = getattr(self, "request_capacity_publish", None)
        if publish is not None:
            publish()

    def _record_upstream_outcome(self, outcome, error=None):
        """Record one terminal provider-I/O outcome across nested owners."""
        if error is not None and getattr(error, "_gateway_upstream_outcome_recorded", False):
            return
        if error is not None:
            try:
                error._gateway_upstream_outcome_recorded = True
            except (AttributeError, TypeError):
                pass
        self._record_stage_outcome("upstream", outcome)

    @staticmethod
    def _stage_error(error, stage):
        if isinstance(error, GatewayError):
            error.stage = stage
        return error

    async def stop_capacity_publisher(self, timeout=2.0):
        """Stop, flush best-effort, and join the publisher task."""
        task = getattr(self, "_capacity_task", None)
        if task is None:
            return
        self._capacity_stopping = True
        self._capacity_dirty = True
        if self._capacity_event is not None:
            self._capacity_event.set()
        bounded = max(0.01, timeout)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=bounded)
        except asyncio.TimeoutError:
            # Cancellation is bounded too: a broken writer must not hold up
            # server shutdown indefinitely. The task remains owned until its
            # done callback clears the reference if it resists cancellation.
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=bounded)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if not task.done():
                    # Do not claim shutdown completed while a cancellation-
                    # resistant writer is still alive. The task remains
                    # referenced by the Engine so its owner can join it
                    # before closing the event loop.
                    raise TimeoutError("capacity publisher did not stop")
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            if task.done():
                self._capacity_task = None
                self._capacity_event = None
                self._capacity_dirty = False

    async def codex_tokens(self, route):
        row = await self.credentials._record(route.credential_id)
        if row["health"] != "active" or not row["token_expires_at"] or row["token_expires_at"] <= datetime.now(timezone.utc):
            raise GatewayError("reauth_required", 503)
        return row["account_id"], self.credentials._tokens(row)

    async def credential(self, route):
        async with self.db.sessions() as session:
            cipher = await session.scalar(text("SELECT encrypted_secret FROM credentials WHERE id=:id AND enabled "
                                              "AND provider_id=:provider"), {"id": route.credential_id, "provider": route.provider_id})
        if cipher is None:
            raise GatewayError("upstream_unavailable", 503)
        return self.vault.open("credential", route.credential_id, Ciphertext.from_dict(cipher)).decode()

    async def session_digest(self, principal, model, value):
        if value is None:
            return None
        model_id = await self.catalog.resolve_model(principal, model)
        return routing_session.session_digest(self.digest_key, principal, model_id, value)

    @asynccontextmanager
    async def route_resources(self, route, selection, owner, deadline):
        """Admit upstream capacity before claiming a scarce proxy lease.

        Provider admission is the bounded resource that decides whether a
        request may dispatch.  Acquiring a proxy first lets a burst of
        rejected requests occupy every proxy lease while waiting for provider
        slots, which in turn consumes database connections and can starve
        health/admin traffic.  The provider slot is therefore acquired first;
        the proxy is claimed only after admission succeeds and is released
        before the provider admission is released.
        """
        while True:
            if isinstance(deadline, datetime) and datetime.now(timezone.utc) >= deadline:
                raise GatewayError("deadline_exceeded", 504, "request")
            proxy_capacity = False
            try:
                # Waiting for provider capacity and waiting for a local proxy
                # are independent resources. If proxy capacity is exhausted,
                # this context exits before the bounded retry, releasing the
                # provider admission rather than holding it while sleeping.
                provider_args = {"wait": True} if "wait" in inspect.signature(self.provider_limits.acquire).parameters else {}
                proxy_acquire = getattr(self.proxies, "acquire", None)
                proxy_args = ({"wait": True} if proxy_acquire is not None
                              and "wait" in inspect.signature(proxy_acquire).parameters else {})
                provider_episode = self._begin_resource_wait("provider")
                provider_waiting = True
                provider_outcome = "success"
                try:
                    provider_context = self.provider_limits.acquire(route, deadline, **provider_args)
                    async with provider_context:
                        self._end_resource_wait(provider_episode, "success")
                        provider_episode = None
                        provider_waiting = False
                        # Direct routes have no scarce proxy resource to wait
                        # for, so they must not create a misleading zero-time
                        # proxy wait episode.
                        proxy_episode = (None if getattr(selection, "mode", None) == "direct"
                                         else self._begin_resource_wait("proxy"))
                        proxy_waiting = getattr(selection, "mode", None) != "direct"
                        proxy_outcome = "success"
                        try:
                            # Never let proxy-capacity polling pin a provider slot.
                            # A non-waiting claim is enough to preserve the safe
                            # provider-before-proxy ordering for this attempt.
                            async with self.proxies.acquire(selection, owner, deadline,
                                                             **({"wait": False} if proxy_args else {})) as lease:
                                self._end_resource_wait(proxy_episode, "success")
                                proxy_episode = None
                                proxy_waiting = False
                                await self.transport._validate(route, lease.proxy)
                                yield lease
                            return
                        except asyncio.CancelledError:
                            if proxy_waiting:
                                proxy_outcome = "cancelled"
                                self._record_stage_outcome("proxy", "cancelled")
                            else:
                                self._record_upstream_outcome("cancelled")
                            raise
                        except ProxyCapacityError:
                            proxy_outcome = "failed"
                            raise
                        except GatewayError as error:
                            if proxy_waiting:
                                proxy_outcome = "expired" if error.code == "deadline_exceeded" else "failed"
                                if error.code == "deadline_exceeded":
                                    self._record_stage_outcome("proxy", "expired")
                                    self._stage_error(error, "proxy")
                                elif error.code == "rate_limited":
                                    self._record_stage_outcome("proxy", "rate_limited")
                                else:
                                    self._record_stage_outcome("proxy", "failed")
                            else:
                                outcome = ("expired" if error.code == "deadline_exceeded" else
                                           "rate_limited" if error.code == "rate_limited" else "failed")
                                self._record_upstream_outcome(outcome, error)
                                if error.code == "deadline_exceeded":
                                    self._stage_error(error, "upstream")
                            raise
                        except BaseException:
                            if proxy_waiting:
                                proxy_outcome = "failed"
                                self._record_stage_outcome("proxy", "failed")
                            else:
                                self._record_upstream_outcome("failed")
                            raise
                        finally:
                            self._end_resource_wait(proxy_episode, proxy_outcome)

                except asyncio.CancelledError:
                    if provider_waiting:
                        provider_outcome = "cancelled"
                        self._record_stage_outcome("provider", "cancelled")
                    raise
                except GatewayError as error:
                    if provider_waiting:
                        provider_outcome = "expired" if error.code == "deadline_exceeded" else "failed"
                        if error.code == "deadline_exceeded":
                            self._record_stage_outcome("provider", "expired")
                            self._stage_error(error, "provider")
                        elif error.code == "rate_limited":
                            self._record_stage_outcome("provider", "rate_limited")
                        else:
                            self._record_stage_outcome("provider", "failed")
                    raise
                except BaseException:
                    if provider_waiting:
                        provider_outcome = "failed"
                        self._record_stage_outcome("provider", "failed")
                    raise
                finally:
                    self._end_resource_wait(provider_episode, provider_outcome)

            except ProxyCapacityError:
                proxy_capacity = True
            if proxy_capacity:
                if isinstance(deadline, datetime) and datetime.now(timezone.utc) >= deadline:
                    self._record_stage_outcome("proxy", "expired")
                    raise GatewayError("deadline_exceeded", 504, "proxy") from None
                waiter = getattr(self.proxies, "wait_for_capacity", None)
                wait_episode = self._begin_resource_wait("proxy")
                wait_outcome = "success"
                try:
                    if waiter is not None:
                        remaining = max(0, (deadline - datetime.now(timezone.utc)).total_seconds())
                        if remaining <= 0:
                            raise GatewayError("deadline_exceeded", 504, "proxy")
                        try:
                            await asyncio.wait_for(waiter(selection, owner, deadline), remaining)
                        except asyncio.TimeoutError:
                            raise GatewayError("deadline_exceeded", 504, "proxy") from None
                    else:
                        # Compatibility fallback for test/dialect proxy stores
                        # predating the explicit wait path.  No resource is held
                        # during this bounded sleep.
                        await asyncio.sleep(min(0.05, max(0, (deadline - datetime.now(timezone.utc)).total_seconds())))
                    self._end_resource_wait(wait_episode, "success")
                    wait_episode = None
                except asyncio.CancelledError:
                    wait_outcome = "cancelled"
                    self._record_stage_outcome("proxy", "cancelled")
                    raise
                except GatewayError as error:
                    wait_outcome = "expired" if error.code == "deadline_exceeded" else "failed"
                    if error.code == "deadline_exceeded":
                        self._record_stage_outcome("proxy", "expired")
                        self._stage_error(error, "proxy")
                    elif error.code == "rate_limited":
                        self._record_stage_outcome("proxy", "rate_limited")
                    else:
                        self._record_stage_outcome("proxy", "failed")
                    raise
                except BaseException:
                    wait_outcome = "failed"
                    self._record_stage_outcome("proxy", "failed")
                    raise
                finally:
                    self._end_resource_wait(wait_episode, wait_outcome)

    def meta(self, body, idempotency_key=None, *, protocol="openai", session_digest=None):
        now = datetime.now(timezone.utc)
        claim, payload = None, None
        if idempotency_key is not None:
            if not self.digest_key or not 1 <= len(idempotency_key) <= 200:
                raise GatewayError("invalid_request")
            claim = hmac.new(self.digest_key, b"abgw:claim:"+idempotency_key.encode(), hashlib.sha256).digest()
            payload = hmac.new(self.digest_key, b"abgw:payload:"+json.dumps(body, sort_keys=True,
                               separators=(",", ":")).encode(), hashlib.sha256).digest()
        return RequestMeta(uuid4(), now, now+timedelta(seconds=180), claim, payload, protocol, session_digest)

    async def count_tokens(self, principal, request, meta):
        request = request.model_copy(update={"model": await self.catalog.resolve_model(principal, request.model)})
        routes = await self.catalog.candidates(principal, request, canonical_model=True)
        route = next((r for r in routes if hasattr(self.adapters.get(r.adapter), "count")), None)
        if route is None:
            raise GatewayError("unsupported_feature")
        if route.budget_id is not None:
            # A provider charging for counting needs an explicit cost policy.
            raise GatewayError("unsupported_feature")
        validate_request(request, route)
        await self.ledger.reserve(Admission(meta.request_id, principal, route.public_model_id, Bounds(0, 0), 0, 0,
                                           meta.deadline, protocol=meta.protocol))
        selection = ProxySelection("direct")
        dispatched = False
        known_rejection = None
        attempt = None
        try:
            if route.proxy_profile_id is not None:
                if self.proxy_resolver is None:
                    raise GatewayError("proxy_not_ready", 503)
                selection = await self.proxy_resolver(route.proxy_profile_id)
            async with self.route_resources(route, selection, meta.request_id, meta.deadline) as lease:
                await self.credential(route)
                await routing_session.revalidate(self, principal, request, route)
                attempt = uuid4()
                await self.ledger.mark_dispatched(meta.request_id, attempt, route=route)
                dispatched = True
                try:
                    # The attribution transaction can wait on configuration
                    # writers. Fence again before any provider HTTP, just as
                    # generation does, and retain proof of zero dispatch.
                    try:
                        await routing_session.revalidate(self, principal, request, route)
                    except BaseException:
                        known_rejection = attempt
                        raise
                    count = await self.adapters[route.adapter].count(request, route, lease)
                except UpstreamRejected as exc:
                    known_rejection = attempt
                    received_at = time.monotonic()
                    await self.ledger.mark_rejected(meta.request_id, attempt, "rejected_before_generation")
                    dispatched = False
                    if exc.upstream_status == 429:
                        await self.catalog.cooldown(route.provider_id, None,
                                                    exc.retry_after if exc.retry_after is not None else 60,
                                                    started_at=received_at)
                        if exc.retry_after is not None:
                            exc.retry_after = await self.catalog.retry_after(principal, request, binding_id=route.binding_id)
                    raise
                await self.ledger.settle(meta.request_id, Usage(0, 0), attempt_id=attempt)
                return count
        except BaseException:
            if known_rejection is not None:
                await self.ledger.reject_and_release(meta.request_id, known_rejection)
            elif dispatched:
                await self.ledger.mark_pending(meta.request_id, "interrupted")
            else:
                state = await self.ledger.request_state(meta.request_id) if hasattr(self.ledger, "request_state") else None
                if state == "dispatched":
                    await self.ledger.reconcile_interrupted(meta.request_id, attempt)
                else:
                    await self.ledger.release_unspent(meta.request_id, "not_dispatched")
            raise

    async def prepare(self, principal, request, meta):
        selection_state = await routing_session.select(self, principal, request, meta)
        request, routes, scope = selection_state.request, selection_state.routes, selection_state.scope
        visible_model = await self.catalog.visible_model(principal, scope.public_model_id)
        route = routes[0]
        if route.adapter not in self.adapters:
            raise GatewayError("upstream_unavailable", 503)
        codex = route.adapter == "codex_oauth"
        if codex:
            # Each account once, same provider, finite budget (at most eight).
            # The pool itself is not truncated: subsequent requests rotate its
            # starting account through the shared catalog cursor.
            unique = {}
            for candidate in routes:
                if candidate.provider_id == route.provider_id and candidate.adapter == "codex_oauth":
                    unique.setdefault(candidate.credential_id, candidate)
            limit = 1 if selection_state.pinned else selection_state.retry_limit + 1
            routes = tuple(unique.values())
        dispatch_routes = routes[:limit] if codex else routes[:2]
        # Codex strips client output-limit hints; they cannot lower admission
        # or retry reservations below the binding's certified output bound.
        max_output = route.bounds.output_tokens if codex else request.options.max_output_tokens or min(4096, route.bounds.output_tokens)
        if max_output > route.bounds.output_tokens:
            raise GatewayError("invalid_request")
        if not codex:
            request = request.model_copy(update={"options": request.options.model_copy(update={"max_output_tokens": max_output})})
        validate_request(request,route)
        bounds = Bounds(route.bounds.input_tokens, max_output)
        deadline = min(meta.deadline, datetime.now(timezone.utc)+timedelta(seconds=route.timeout))
        hold = await self.ledger.reserve(Admission(meta.request_id, principal, route.public_model_id, bounds,
            route.input_micro, route.output_micro, deadline, meta.idempotency_digest, meta.payload_digest, meta.protocol,
            cache_read_micro=route.cache_read_micro, cache_write_micro=route.cache_write_micro))
        stack = AsyncExitStack()
        admission_stack = AsyncExitStack()
        dispatched = False
        budget_attempt = None
        known_rejection = None
        hinted_providers = set()
        hinted_accounts = set()
        transport_retry_used = False
        attempt = None
        try:
            # Keep admission across per-attempt retries; it is released only
            # when the prepared call (or preparation cleanup) fully closes.
            await admission_stack.enter_async_context(self.admission.enter(deadline))
            publish = getattr(self, "request_capacity_publish", None)
            if publish is not None:
                publish()
            for number, selected in enumerate(dispatch_routes):
                known_rejection = None
                if datetime.now(timezone.utc) >= deadline:
                    raise GatewayError("deadline_exceeded", 504, "request")
                attempt_output = selected.bounds.output_tokens if codex else max_output
                validate_request(request,selected)
                if selected.adapter not in self.adapters or attempt_output > selected.bounds.output_tokens:
                    raise GatewayError("upstream_unavailable", 503)
                if (selected.adapter == "codex_oauth") != codex:
                    raise GatewayError("unsupported_feature")
                if number:
                    hold = await self.ledger.resize(hold, Bounds(selected.bounds.input_tokens, attempt_output))
                selection = ProxySelection("direct")
                stamp = None
                if codex:
                    try:
                        await self.credentials.fresh_tokens(selected.credential_id, deadline)
                    except GatewayError as exc:
                        # No inference was sent. A failed refresh fences that
                        # credential, not the rest of an unpinned account pool.
                        if exc.code not in {"reauth_required", "refresh_uncertain", "not_found"}:
                            raise
                        if exc.code != "not_found":
                            await self.catalog.credential_outcome(selected.provider_id, selected.credential_id, exc.code)
                        if number + 1 == len(dispatch_routes):
                            raise
                        continue
                    selection, stamp = await AccountProxyResolver(self.db, self.proxy_resolver).policy(selected.credential_id)
                elif selected.proxy_profile_id is not None:
                    if self.proxy_resolver is None:
                        raise GatewayError("proxy_not_ready", 503, "proxy")
                    selection = await self.proxy_resolver(selected.proxy_profile_id)
                lease = await stack.enter_async_context(
                    self.route_resources(selected, selection, meta.request_id, deadline))
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
                    upper = (selected.bounds.input_tokens*max(rates)+attempt_output*price.output_per_million)/1_000_000
                    await self.budgets.reserve(selected.budget_id, attempt, price.currency,
                                               upper.quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING),request_id=meta.request_id)
                    budget_attempt = attempt
                await routing_session.revalidate(self, principal, request, selected, stamp)
                await self.ledger.mark_dispatched(meta.request_id, attempt, route=selected)
                dispatched = True
                upstream_io_started = False
                try:
                    # Attribution may wait on FK/config writers. Fence again
                    # after its committed transaction and before provider I/O.
                    try:
                        await routing_session.revalidate(self, principal, request, selected, stamp)
                    except BaseException:
                        known_rejection = attempt
                        await self.ledger.mark_rejected(meta.request_id, attempt, "rejected_before_generation")
                        dispatched = False
                        raise
                    upstream_io_started = True
                    stream = await stack.enter_async_context(self.adapters[selected.adapter].open(request, selected, lease))
                except UpstreamRejected as exc:
                    known_rejection = attempt
                    received_at = getattr(exc, "received_at", time.monotonic())
                    if exc.upstream_status == 429 and exc.retry_after is not None:
                        hinted_providers.add(selected.provider_id)
                        hinted_accounts.add(selected.credential_id)
                    await self.ledger.mark_rejected(meta.request_id, attempt, "rejected_before_generation")
                    dispatched = False
                    if codex:
                        if exc.upstream_status == 401:
                            await self.credentials.expire_rejected_access(
                                selected.credential_id, getattr(exc, 'access_token_digest', None), started_at=received_at)
                        else:
                            await self.catalog.credential_outcome(selected.provider_id, selected.credential_id, exc.code)
                    if budget_attempt is not None:
                        await self.budgets.settle(budget_attempt, Decimal(0))
                        budget_attempt = None
                    if exc.upstream_status == 429:
                        await self.catalog.cooldown(selected.provider_id, selected.credential_id if codex else None,
                                                    exc.retry_after if exc.retry_after is not None else 60,
                                                    started_at=received_at)
                    elif codex and exc.code == "model_not_supported":
                        await self.catalog.model_cooldown(selected, 300, started_at=received_at)
                    await stack.aclose()
                    stack = AsyncExitStack()
                    terminal = (not exc.safe_retry or number + 1 == len(dispatch_routes)
                                or selection_state.pinned
                                or not codex and routes[1].provider_id == selected.provider_id)
                    if terminal:
                        if exc.upstream_status == 429:
                            # No promise for untried/unhinted providers. Re-read
                            # local deadlines, including routes already cooling at
                            # selection and longer concurrent cooldown updates.
                            hints_complete = ({r.credential_id for r in routes} <= hinted_accounts if codex
                                              else {r.provider_id for r in routes} <= hinted_providers)
                            if hints_complete:
                                exc.retry_after = await self.catalog.retry_after(
                                    principal, request, binding_id=selected.binding_id if selection_state.pinned else None)
                            else:
                                exc.retry_after = None
                        self._record_upstream_outcome(
                            "rate_limited" if exc.upstream_status == 429 else "failed", exc)
                        raise
                    known_rejection = None
                    continue
                except asyncio.CancelledError as exc:
                    if known_rejection is None and upstream_io_started:
                        self._record_upstream_outcome("cancelled", exc)
                    raise
                except GatewayError as exc:
                    # A transport connect failure through a configured proxy
                    # is retryable only when response headers were never
                    # acquired.  Close the per-attempt stack first so the
                    # provider admission and proxy lease cannot pin capacity
                    # while selecting the next untried route.
                    retryable = (not transport_retry_used and getattr(exc, "safe_retry", False)
                                 and getattr(exc, "transport_phase", None) == "before_response"
                                 and getattr(exc, "proxy_used", False))
                    if not retryable:
                        if upstream_io_started:
                            outcome = ("expired" if exc.code == "deadline_exceeded" else
                                       "rate_limited" if exc.code == "rate_limited" else "failed")
                            self._record_upstream_outcome(outcome, exc)
                        raise
                    known_rejection = attempt
                    await self.ledger.mark_rejected(meta.request_id, attempt, "rejected_before_generation")
                    dispatched = False
                    if budget_attempt is not None:
                        await self.budgets.settle(budget_attempt, Decimal(0))
                        budget_attempt = None
                    await stack.aclose()
                    stack = AsyncExitStack()
                    transport_retry_used = True
                    if (number + 1 == len(dispatch_routes) or selection_state.pinned
                            or not codex and routes[1].provider_id == selected.provider_id):
                        if upstream_io_started:
                            self._record_upstream_outcome("failed", exc)
                        raise
                    known_rejection = None
                    continue
                except BaseException as exc:
                    if known_rejection is None and upstream_io_started:
                        self._record_upstream_outcome("failed", exc)
                    raise
                prepared = PreparedCall(self, stack, stream, meta.request_id, selected, attempt, budget_attempt, scope,
                                        admission_stack=admission_stack)
                prepared.collector.model = visible_model
                return prepared
            raise GatewayError("upstream_unavailable", 503)
        except BaseException as error:
            failure_code = error.code if isinstance(error, GatewayError) else "upstream_error"
            async def cleanup():
                try:
                    no_generation = known_rejection is not None
                    if budget_attempt is not None:
                        await self.budgets.settle(budget_attempt, None if dispatched and not no_generation else Decimal(0))
                    if no_generation:
                        await self.ledger.reject_and_release(meta.request_id, known_rejection)
                    elif dispatched:
                        await self.ledger.mark_pending(meta.request_id, "interrupted")
                        if codex:
                            await self.catalog.credential_outcome(selected.provider_id, selected.credential_id, failure_code)
                    else:
                        state = await self.ledger.request_state(meta.request_id) if hasattr(self.ledger, "request_state") else None
                        if state == "dispatched":
                            await self.ledger.reconcile_interrupted(meta.request_id, attempt)
                        else:
                            await self.ledger.release_unspent(meta.request_id, "not_dispatched")
                finally:
                    # Admission is an independent resource.  Always release
                    # it even when provider/proxy/stream cleanup fails; a
                    # cleanup error must not permanently consume a gate slot.
                    try:
                        await stack.aclose()
                    finally:
                        await admission_stack.aclose()
                        publish = getattr(self, "request_capacity_publish", None)
                        if publish is not None:
                            publish()
            owned = asyncio.create_task(asyncio.wait_for(cleanup(), 5))
            cancelled = isinstance(error, asyncio.CancelledError)
            try:
                while not owned.done():
                    try:
                        await asyncio.shield(owned)
                    except asyncio.CancelledError:
                        cancelled = True
                await owned
            except asyncio.TimeoutError:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise GatewayError("deadline_exceeded", 504, "request") from None
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise GatewayError("storage_unavailable", 503, "storage") from None
            if cancelled:
                raise asyncio.CancelledError
            raise


class PreparedCall:
    def __init__(self, engine, stack, stream, request_id, route, attempt_id, budget_attempt=None, scope=None,
                 admission_stack=None, cleanup_grace=5.0):
        self.engine, self.stack, self.stream = engine, stack, stream
        self.admission_stack = admission_stack
        self.request_id, self.route, self.attempt_id = request_id, route, attempt_id
        self.budget_attempt = budget_attempt
        self.scope = scope
        self.collector = EventCollector("chatcmpl-"+request_id.hex, route.public_model_id)
        self.closed = self.settled = self.started = False
        self._close_task = None
        self.cleanup_grace = cleanup_grace
        self._cleanup_children = set()

    async def _save_usage(self, usage):
        cost = estimate_cost(usage, self.route.cost_schedule)
        if self.budget_attempt is not None:
            await self.engine.budgets.settle(self.budget_attempt, cost.amount.quantize(Decimal("0.000000000001")) if cost else None)
        async with self.engine.db.sessions.begin() as session:
            await session.execute(text("UPDATE attempts SET usage=CAST(:usage AS jsonb),cost=CAST(:cost AS jsonb),status='completed' WHERE id=:id"),
                                  {"usage": json.dumps(asdict(usage)), "id": self.attempt_id,
                                   "cost": json.dumps({"amount": str(cost.amount), "currency": cost.currency, "source": cost.source}) if cost else None})
            bound=await session.execute(text('SELECT input_bound,output_bound FROM requests WHERE id=:id'),{'id':self.request_id})
            input_bound,output_bound=bound.one()
            if usage.input_tokens>input_bound or usage.output_tokens>output_bound:
                await session.execute(text("UPDATE model_bindings SET config=jsonb_set(config,'{enabled}','false'::jsonb),version=version+1 WHERE id=:id"),{'id':self.route.binding_id})
                await self.engine.catalog._audit(session,'binding.quarantined.usage_bound',self.route.binding_id)
        if self.route.adapter == "codex_oauth":
            await self.engine.catalog.credential_outcome(self.route.provider_id, self.route.credential_id)

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
            self.engine._record_upstream_outcome("failed")
            if self.route.adapter == "codex_oauth":
                await self.engine.catalog.credential_outcome(self.route.provider_id, self.route.credential_id, "upstream_error")
            raise GatewayError("upstream_error", 502, "stream") from None
        except asyncio.CancelledError as error:
            self.engine._record_upstream_outcome("cancelled", error)
            raise
        except GeneratorExit as error:
            self.engine._record_upstream_outcome("cancelled", error)
            raise
        except GatewayError as error:
            outcome = ("expired" if error.code == "deadline_exceeded" else
                       "rate_limited" if error.code == "rate_limited" else "failed")
            self.engine._record_upstream_outcome(outcome, error)
            if self.route.adapter == "codex_oauth":
                await self.engine.catalog.credential_outcome(self.route.provider_id, self.route.credential_id, error.code)
            raise
        except BaseException as error:
            self.engine._record_upstream_outcome("failed", error)
            raise
        finally:
            await self.close()

    async def collect(self):
        async for _ in self.events():
            pass
        return self.collector.result()

    async def close(self):
        # Generator finalization and HTTP disconnect may both call close. All
        # callers join the same persistent supervisor. A bounded caller timeout
        # never cancels or detaches the supervisor; a later close can drain it.
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_once(), name="prepared-call-cleanup")
            tasks = getattr(self.engine, "_prepared_cleanup_tasks", None)
            if tasks is None:
                tasks = self.engine._prepared_cleanup_tasks = set()
            tasks.add(self._close_task)
            self._close_task.add_done_callback(self._observe_cleanup)
        try:
            await asyncio.wait_for(asyncio.shield(self._close_task), self.cleanup_grace)
        except asyncio.TimeoutError:
            raise TimeoutError("interrupted cleanup exceeded bounded grace") from None

    def _observe_cleanup(self, task):
        # A caller may time out before the owned supervisor finishes. Retrieve
        # its terminal exception to avoid an unhandled-task warning; awaiting
        # the same task later still propagates that exact exception.
        if not task.cancelled():
            task.exception()
        registry = getattr(self.engine, "_prepared_cleanup_tasks", None)
        if registry is not None:
            registry.discard(task)

    async def _close_once(self):
        async def accounting_cleanup():
            if not self.settled:
                if self.budget_attempt is not None:
                    await self.engine.budgets.settle(self.budget_attempt, None)
                reconcile = getattr(self.engine.ledger, "reconcile_interrupted", None)
                if reconcile is not None:
                    # Keep the interruption marker durable even when the
                    # authoritative attempt receipt arrives slightly later.
                    # The reconciliation transaction then upgrades this same
                    # request exactly once if that receipt is already saved.
                    mark_pending = getattr(self.engine.ledger, "mark_pending", None)
                    if mark_pending is not None:
                        await mark_pending(self.request_id, "interrupted")
                    await reconcile(self.request_id, self.attempt_id)
                else:
                    await self.engine.ledger.mark_pending(self.request_id, "interrupted")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cleanup_grace

        async def resource_cleanup():
            # The provider stream and transport stack own the same underlying
            # response in normal operation.  Never close them concurrently:
            # cancellation must complete before the AsyncExitStack releases
            # its adapter/response and route resources. Admission is released
            # last, and remains held when a cancellation-resistant child does
            # not unwind.
            try:
                await self.stream.cancel()
            finally:
                try:
                    await self.stack.aclose()
                finally:
                    if self.admission_stack is not None:
                        await self.admission_stack.aclose()

        resource_child = asyncio.create_task(resource_cleanup(), name="prepared-resource-cleanup")

        children = [asyncio.create_task(accounting_cleanup(), name="prepared-accounting-cleanup"),
                    resource_child]
        self._cleanup_children.update(children)
        for child in children:
            child.add_done_callback(self._cleanup_children.discard)
        _, pending = await asyncio.wait(children, timeout=max(0, deadline - loop.time()))
        for child in pending:
            child.cancel()
        # The supervisor deliberately remains live while cancellation-resistant
        # dependencies unwind. PreparedCall retains this task and every child;
        # later close calls rejoin the same ownership graph.
        results = await asyncio.gather(*children, return_exceptions=True)
        for child in children:
            self._cleanup_children.discard(child)
        publish = getattr(self.engine, "request_capacity_publish", None)
        if publish is not None:
            publish()
        failures = [result for result in results if isinstance(result, BaseException)]
        self.closed = not failures
        if failures:
            original = next((failure for failure in failures if not isinstance(failure, asyncio.CancelledError)), None)
            if original is not None:
                raise original
            raise TimeoutError("interrupted cleanup exceeded bounded grace")
