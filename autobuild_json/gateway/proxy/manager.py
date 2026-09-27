import asyncio
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
        Exception.__init__(self, "proxy_capacity")
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


class ProxyManager:
    def __init__(self, store, kiot=None, *, acquisition_timeout=10, heartbeat_interval=1):
        self.store, self.kiot = store, kiot or KiotClient()
        self.acquisition_timeout = min(10, acquisition_timeout)
        self.heartbeat_interval = min(1, heartbeat_interval)
        self._cursor = 0
        self._wait_attempts = 0
        self._capacity_signals = 0

    def snapshot(self):
        return {"wait_attempts": self._wait_attempts, "capacity_signals": self._capacity_signals}

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

    async def _cleanup(self, tokens):
        failure = None
        for token in reversed(tokens):
            try:
                await self.store.release(token)
            except Exception as exc:
                # Try every slot even if one release fails. Report the first
                # failure afterward; cancellation still respects our timeout.
                if failure is None:
                    failure = exc
        if failure is not None:
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
                        if await self.store.is_disabled(resource):
                            last_error = GatewayError("kiot_key_invalid", 400, "proxy")
                            continue
                        if resource in examined:
                            continue
                        all_examined = False
                        token = await self.store.claim(resource, owner, deadline)
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
                                await self._cleanup(tokens)
                                tokens.clear()
                                raise
                            if exc.code == "kiot_unavailable":
                                await self._cleanup(tokens)
                                tokens.clear()
                                raise
                            if exc.code == "kiot_key_invalid":
                                invalid_seen = True
                                await self.store.disable_resource(resource)
                            await self._cleanup(tokens)
                            tokens.clear()
                            continue
                        endpoint = await self.store.claim(endpoint_resource(resolved), owner, deadline)
                        if endpoint is None:
                            capacity_seen = True
                            await self._cleanup(tokens)
                            tokens.clear()
                            continue
                        tokens.append(endpoint)
                        proxy = resolved
                    else:
                        all_examined = False
                        endpoint = await self.store.claim(endpoint_resource(entry), owner, deadline)
                        if endpoint is None:
                            capacity_seen = True
                            continue
                        tokens.append(endpoint)
                        proxy = entry
                    if proxy is not None:
                        break
                if proxy is None:
                    if all_examined and not (wait and capacity_seen):
                        break
                    if wait:
                        self._wait_attempts += 1
                    await asyncio.sleep(min(0.05, max(0, acquire_end-time.monotonic())))
            if proxy is None:
                if wait and capacity_seen:
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
                            if not await self.store.renew(token):
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
                # wait_for owns, cancels and joins its cleanup coroutine. Shielding
                # an anonymous coroutine here leaks it on timeout/cancellation.
                await asyncio.wait_for(self._cleanup(tokens), timeout=5)

    async def release_kiot(self, key, actor):
        if not actor:
            raise GatewayError("permission_denied", 403)
        token = await self.store.claim("kiot:"+key.fingerprint, uuid4(), datetime.now(timezone.utc)+timedelta(seconds=15))
        if token is None:
            raise GatewayError("proxy_not_ready", 503, "proxy")
        try:
            await self.kiot.out(key.secret, deadline=time.monotonic()+10)
        finally:
            await self.store.release(token)
