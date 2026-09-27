import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import time
from uuid import uuid4

from ..errors import GatewayError
from .kiot import KiotClient, lease_action


class ProxyCapacityError(GatewayError):
    """Internal signal that a local lease was busy and may be retried."""
    def __init__(self):
        # Direct manager callers historically surface this exception string;
        # keep that public contract while Engine keys retry behavior on the
        # private subclass and internal code below.
        Exception.__init__(self, "proxy_not_ready")
        self.code = "proxy_capacity"
        self.status = 503
        self.stage = "proxy"
        self.retry_after = None

    def to_dict(self):
        # This signal is consumed inside routing and must never become a
        # public error code.
        return {"error": {"code": "proxy_not_ready", "message": "proxy_not_ready", "stage": "proxy"}}


@dataclass(frozen=True)
class ProxyLease:
    proxy: object
    owner: object
    generation: int
    deadline: datetime


def endpoint_resource(proxy):
    # Same server reached with different credential/key records still shares a slot.
    return "endpoint:" + hashlib.sha256(proxy.server.encode()).hexdigest()


class _ResourceLockEntry:
    __slots__ = ("lock", "users")

    def __init__(self):
        self.lock = asyncio.Lock()
        self.users = 0


class ProxyManager:
    _CLEANUP_GRACE_SECONDS = 0.25
    _RESOURCE_LOCK_CAPACITY = 256

    def __init__(self, store, kiot=None, *, acquisition_timeout=10, heartbeat_interval=1):
        self.store, self.kiot = store, kiot or KiotClient()
        self.acquisition_timeout = min(10, acquisition_timeout)
        self.heartbeat_interval = min(1, heartbeat_interval)
        self._cursor = 0
        self._wait_attempts = 0
        self._capacity_signals = 0
        self._waits = deque(maxlen=2048)
        self._last_wait_ms = 0.0
        self._cleanup_failures = 0
        self._resource_locks = {}
        self._resource_lock_overflow = tuple(_ResourceLockEntry() for _ in range(32))
        self._resource_overflow_keys = {}

    def snapshot(self):
        waits = sorted(self._waits)
        def percentile(percent):
            if not waits:
                return 0.0
            from math import ceil
            return waits[max(0, ceil(len(waits) * percent) - 1)]
        return {"wait_attempts": self._wait_attempts, "capacity_signals": self._capacity_signals,
                "wait_p50_ms": percentile(.50), "wait_p95_ms": percentile(.95),
                "last_wait_ms": self._last_wait_ms, "cleanup_failures": self._cleanup_failures}

    def _resource_lock(self, resource):
        overflow = self._resource_overflow_keys.get(resource)
        if overflow is not None:
            overflow.users += 1
            return overflow
        entry = self._resource_locks.get(resource)
        if entry is not None:
            entry.users += 1
            return entry
        if len(self._resource_locks) >= self._RESOURCE_LOCK_CAPACITY:
            for key, candidate in tuple(self._resource_locks.items()):
                if (candidate.users == 0 and not candidate.lock.locked()
                        and not getattr(candidate.lock, "_waiters", None)):
                    del self._resource_locks[key]
                    break
        if len(self._resource_locks) >= self._RESOURCE_LOCK_CAPACITY:
            entry = self._resource_overflow_keys.get(resource)
            if entry is None:
                digest = hashlib.sha256(resource.encode()).digest()
                entry = self._resource_lock_overflow[int.from_bytes(digest[:4]) % len(self._resource_lock_overflow)]
                # Active overflow assignments are bounded by process admission;
                # retain every key until its reservation is released.
                self._resource_overflow_keys[resource] = entry
        else:
            entry = _ResourceLockEntry()
            self._resource_locks[resource] = entry
        entry.users += 1
        return entry

    def _resource_unlock(self, resource, entry):
        entry.users -= 1
        if entry.users == 0 and self._resource_overflow_keys.get(resource) is entry:
            del self._resource_overflow_keys[resource]

    @staticmethod
    def _close_operation(operation):
        close = getattr(operation, "close", None)
        if close is not None:
            close()

    async def _bounded_store_call(self, operation, acquire_end):
        remaining = acquire_end - time.monotonic()
        if remaining <= 0:
            self._close_operation(operation)
            raise ProxyCapacityError()
        try:
            return await asyncio.wait_for(operation, remaining)
        except asyncio.TimeoutError:
            self._close_operation(operation)
            # A locked lease row is local capacity contention.  Convert the
            # acquisition cap expiry into the same retryable signal so the
            # provider context is released before Engine waits again.
            raise ProxyCapacityError() from None

    async def _bounded_resource_call(self, resource, operation, acquire_end):
        entry = self._resource_lock(resource)
        remaining = acquire_end - time.monotonic()
        if remaining <= 0:
            self._close_operation(operation)
            self._resource_unlock(resource, entry)
            raise ProxyCapacityError()
        acquired = False
        try:
            try:
                await asyncio.wait_for(entry.lock.acquire(), remaining)
                acquired = True
            except asyncio.TimeoutError:
                self._close_operation(operation)
                raise ProxyCapacityError() from None
            except BaseException:
                self._close_operation(operation)
                raise
            return await self._bounded_store_call(operation, acquire_end)
        finally:
            if acquired:
                entry.lock.release()
            self._resource_unlock(resource, entry)

    async def wait_for_capacity(self, selection, owner, deadline):
        """Wait for a local lease without retaining any provider admission.

        A successful probe claims and immediately releases one lease.  The
        caller must still perform a fresh claim after acquiring provider
        capacity because another request may win the race.
        """
        started = time.monotonic()
        try:
            while datetime.now(timezone.utc) < deadline:
                try:
                    async with self.acquire(selection, owner, deadline, wait=True):
                        return
                except ProxyCapacityError:
                    continue
            raise GatewayError("deadline_exceeded", 504, "proxy")
        finally:
            wait_ms = (time.monotonic() - started) * 1000
            self._waits.append(wait_ms)
            self._last_wait_ms = wait_ms

    async def _kiot_proxy(self, entry, selection, deadline, acquire_end):
        required = max(0, (deadline-datetime.now(timezone.utc)).total_seconds())
        current = await self.kiot.current(entry.secret, protocol=selection.protocol, deadline=acquire_end)
        action = lease_action(False, current.ttl if current else 0, current.cooldown if current else 0,
                              required, selection.rotate)
        if action == "wait":
            delay = current.cooldown+0.01
            if delay >= acquire_end-time.monotonic():
                raise GatewayError("proxy_not_ready", 503, "proxy", int(current.cooldown)+1)
            await asyncio.sleep(delay)
            action = "new"
        if action == "new":
            try:
                current = await self.kiot.new(entry.secret, protocol=selection.protocol,
                                               region=selection.region, deadline=acquire_end)
            except GatewayError as exc:
                if exc.code != "kiot_allocation_uncertain":
                    raise
                current = await self.kiot.current(entry.secret, protocol=selection.protocol, deadline=acquire_end)
        if current is None or current.ttl < required+15:
            raise GatewayError("proxy_not_ready", 503, "proxy")
        return current.proxy

    async def _cleanup(self, tokens, cleanup_end=None):
        failure = None
        for token in reversed(tokens):
            try:
                operation = self.store.release(token)
                if cleanup_end is None:
                    await operation
                else:
                    await self._bounded_resource_call(token.resource, operation, cleanup_end)
                # Keep failed tokens available to the owner/finally path for
                # a bounded retry.  A timed-out release must never be
                # mistaken for a successful lease release.
                tokens.remove(token)
            except Exception as exc:
                # Try every slot even if one release fails. Report the first
                # failure afterward; cancellation still respects our timeout.
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure

    async def _cleanup_owned(self, tokens, cleanup_end=None):
        await self._cleanup(tokens, cleanup_end)

    async def _cleanup_with_retry(self, tokens, seconds=0.5, *, attempts=2, raise_failure=True):
        end = time.monotonic() + seconds
        failure = None
        for _ in range(attempts):
            if not tokens or time.monotonic() >= end:
                break
            try:
                await self._cleanup(tokens, end)
            except Exception as exc:
                failure = exc
                await asyncio.sleep(0)
        if tokens and failure is not None:
            self._cleanup_failures += 1
        if raise_failure and tokens and failure is not None:
            raise failure

    @asynccontextmanager
    async def acquire(self, selection, owner, deadline, *, wait=False):
        if deadline.tzinfo is None or deadline <= datetime.now(timezone.utc):
            raise GatewayError("deadline_exceeded", 504, "proxy")
        if selection.mode == "direct":
            yield ProxyLease(None, owner, 0, deadline)
            return
        acquire_end = time.monotonic()+min(self.acquisition_timeout, (deadline-datetime.now(timezone.utc)).total_seconds())
        entries = selection.runtime_entries
        offset = self._cursor % len(entries)
        self._cursor += 1
        tokens, proxy = [], None
        last_error = GatewayError("proxy_not_ready", 503, "proxy")
        capacity_seen = False
        invalid_seen = False
        # Each key's provider control API is queried at most once per acquisition;
        # busy local slots may be waited on without repeatedly hitting Kiot.
        examined = set()
        try:
            while time.monotonic() < acquire_end and proxy is None:
                all_examined = True
                for entry in entries[offset:]+entries[:offset]:
                    if selection.mode == "kiotproxy":
                        resource = "kiot:"+entry.fingerprint
                        if await self._bounded_resource_call(resource, self.store.is_disabled(resource), acquire_end):
                            last_error = GatewayError("kiot_key_invalid", 400, "proxy")
                            continue
                        if resource in examined:
                            continue
                        all_examined = False
                        token = await self._bounded_resource_call(resource, self.store.claim(resource, owner, deadline), acquire_end)
                        if token is None:
                            capacity_seen = True
                            continue
                        tokens.append(token)
                        examined.add(resource)
                        try:
                            resolved = await self._kiot_proxy(entry, selection, deadline, acquire_end)
                        except GatewayError as exc:
                            last_error = exc
                            if exc.code == "proxy_not_ready":
                                try:
                                    await self._cleanup_owned(
                                        tokens, max(acquire_end, time.monotonic() + self._CLEANUP_GRACE_SECONDS))
                                except Exception:
                                    pass
                                raise
                            if exc.code == "kiot_unavailable":
                                try:
                                    await self._cleanup_owned(
                                        tokens, max(acquire_end, time.monotonic() + self._CLEANUP_GRACE_SECONDS))
                                except Exception:
                                    pass
                                raise
                            if exc.code == "kiot_key_invalid":
                                invalid_seen = True
                                disable_end = datetime.now(timezone.utc) + timedelta(
                                    seconds=max(0, acquire_end - time.monotonic()))
                                try:
                                    await self._bounded_resource_call(
                                        resource,
                                        self.store.disable_resource(resource, disable_end), acquire_end)
                                except Exception:
                                    # Health persistence is best effort here;
                                    # retain the permanent Kiot classification
                                    # and let the outer cleanup release the
                                    # lease within its grace.
                                    pass
                            cleanup_end = max(acquire_end, time.monotonic() + self._CLEANUP_GRACE_SECONDS)
                            try:
                                await self._cleanup_owned(tokens, cleanup_end)
                            except Exception:
                                # Preserve the primary Kiot error. The outer
                                # owner cleanup retains any unreleased token.
                                if exc.code not in {"proxy_not_ready", "kiot_unavailable", "kiot_key_invalid"}:
                                    raise
                            continue
                        endpoint_resource_id = endpoint_resource(resolved)
                        endpoint = await self._bounded_resource_call(
                            endpoint_resource_id,
                            self.store.claim(endpoint_resource_id, owner, deadline), acquire_end)
                        if endpoint is None:
                            capacity_seen = True
                            await self._cleanup_owned(tokens, acquire_end)
                            continue
                        tokens.append(endpoint)
                        proxy = resolved
                    else:
                        resource = endpoint_resource(entry)
                        if await self._bounded_resource_call(resource, self.store.is_disabled(resource), acquire_end):
                            last_error = GatewayError("proxy_not_ready", 503, "proxy")
                            continue
                        all_examined = False
                        endpoint = await self._bounded_resource_call(resource, self.store.claim(resource, owner, deadline), acquire_end)
                        if endpoint is None:
                            capacity_seen = True
                            continue
                        tokens.append(endpoint)
                        proxy = entry
                    if proxy is not None:
                        break
                if proxy is None:
                    if not wait:
                        # A non-waiting claim is intentionally one pass. The
                        # caller owns provider admission and must not sleep
                        # here; retryable waiting is handled outside it.
                        break
                    if all_examined and not (wait and capacity_seen):
                        break
                    if wait:
                        self._wait_attempts += 1
                    await asyncio.sleep(min(0.05, max(0, acquire_end-time.monotonic())))
            if proxy is None:
                if capacity_seen:
                    self._capacity_signals += 1
                    raise ProxyCapacityError()
                if invalid_seen:
                    raise GatewayError("kiot_key_invalid", 400, "proxy")
                raise last_error
            parent = asyncio.current_task()
            lost = False
            async def heartbeat():
                nonlocal lost
                try:
                    while True:
                        await asyncio.sleep(min(self.heartbeat_interval, max(0.001, (deadline-datetime.now(timezone.utc)).total_seconds())))
                        for token in tokens:
                            renew_end = time.monotonic() + min(
                                self.heartbeat_interval,
                                max(0, (deadline-datetime.now(timezone.utc)).total_seconds()),
                            )
                            if not await self._bounded_resource_call(
                                    token.resource, self.store.renew(token), renew_end):
                                lost = True
                                parent.cancel()
                                return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    lost = True
                    parent.cancel()
            pulse = asyncio.create_task(heartbeat())
            try:
                yield ProxyLease(proxy, owner, tokens[0].generation, deadline)
            except asyncio.CancelledError:
                if lost:
                    raise GatewayError("proxy_error", 502, "proxy") from None
                raise
            finally:
                pulse.cancel()
                await asyncio.gather(pulse, return_exceptions=True)
        finally:
            if tokens:
                # wait_for owns, cancels and joins bounded cleanup retries.
                # Cleanup failure must not overwrite the primary provider or
                # Kiot error from this acquisition. Fencing/hard expiry and
                # the bounded retry remain the durable recovery path.
                try:
                    await asyncio.wait_for(
                        self._cleanup_with_retry(tokens, seconds=.6, attempts=2, raise_failure=False), timeout=.6)
                except asyncio.TimeoutError:
                    self._cleanup_failures += 1

    async def release_kiot(self, key, actor):
        if not actor:
            raise GatewayError("permission_denied", 403)
        resource = "kiot:" + key.fingerprint
        deadline = datetime.now(timezone.utc) + timedelta(seconds=15)
        token = await self._bounded_resource_call(resource, self.store.claim(resource, uuid4(), deadline),
                                                  time.monotonic() + 15)
        if token is None:
            raise GatewayError("proxy_not_ready", 503, "proxy")
        try:
            await self.kiot.out(key.secret, deadline=time.monotonic()+10)
        finally:
            await self._cleanup_with_retry([token], raise_failure=True)
