"""Process-local bounded admission for inference requests."""

import asyncio
from collections import deque
from datetime import datetime, timezone
from math import ceil
from time import monotonic

from .errors import GatewayError


class _Ticket:
    def __init__(self, admission, wait_ms):
        self._admission = admission
        self._released = False
        self.wait_ms = wait_ms

    def release(self):
        if not self._released:
            self._released = True
            self._admission._release()


class _AdmissionContext:
    def __init__(self, admission, deadline):
        self._admission = admission
        self._deadline = deadline
        self._ticket = None

    def __await__(self):
        return self.__aenter__().__await__()

    async def __aenter__(self):
        if self._ticket is None:
            self._ticket = await self._admission._acquire(self._deadline)
        return self._ticket

    async def __aexit__(self, exc_type, exc, tb):
        if self._ticket is not None:
            self._ticket.release()


class InferenceAdmission:
    """Process-local bounded accepted-inference counter; queueing is external."""

    def __init__(self, capacity=100, metrics=None):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self.capacity = capacity
        self._metrics = metrics
        self._condition = asyncio.Condition()
        self._waiters = deque()
        self._inflight = 0
        self._accepted = 0
        self._queue_full = 0
        self._expired = 0
        self._cancelled = 0
        self._waits = []
        self._last_wait_ms = 0.0

    def enter(self, deadline):
        return _AdmissionContext(self, deadline)

    async def _acquire(self, deadline):
        now = datetime.now(deadline.tzinfo or timezone.utc)
        if deadline <= now:
            self._expired += 1
            self._emit_metrics()
            raise GatewayError("deadline_exceeded", 504, "request")

        started = monotonic()
        waiter = object()
        async with self._condition:
            if self._inflight + len(self._waiters) >= self.capacity:
                self._queue_full += 1
                self._emit_metrics()
                raise GatewayError("gateway_busy", 429, "request", retry_after=1)
            self._waiters.append(waiter)
            try:
                while True:
                    now = datetime.now(deadline.tzinfo or timezone.utc)
                    remaining = (deadline - now).total_seconds()
                    if remaining <= 0:
                        self._waiters.remove(waiter)
                        self._expired += 1
                        self._condition.notify_all()
                        self._emit_metrics()
                        raise GatewayError("deadline_exceeded", 504, "request")
                    if self._waiters[0] is waiter and self._inflight < self.capacity:
                        self._waiters.popleft()
                        self._inflight += 1
                        wait_ms = (monotonic() - started) * 1000
                        self._accepted += 1
                        self._waits.append(wait_ms)
                        self._last_wait_ms = wait_ms
                        self._emit_metrics()
                        return _Ticket(self, wait_ms)
                    try:
                        await asyncio.wait_for(self._condition.wait(), remaining)
                    except asyncio.TimeoutError:
                        continue
            except asyncio.CancelledError:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                    self._cancelled += 1
                    self._condition.notify_all()
                    self._emit_metrics()
                raise

    def _release(self):
        if self._inflight:
            self._inflight -= 1

        async def notify():
            async with self._condition:
                self._condition.notify_all()
                self._emit_metrics()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if not loop.is_closed():
            loop.create_task(notify())

    def _emit_metrics(self):
        if self._metrics is not None:
            self._metrics(self.snapshot())

    def snapshot(self):
        waits = sorted(self._waits)

        def percentile(percent):
            if not waits:
                return 0.0
            return waits[max(0, ceil(len(waits) * percent) - 1)]

        return {
            "capacity": self.capacity,
            "inflight": self._inflight,
            "queued": len(self._waiters),
            "accepted": self._accepted,
            "queue_full": self._queue_full,
            "expired": self._expired,
            "cancelled": self._cancelled,
            "wait_p50_ms": percentile(0.50),
            "wait_p95_ms": percentile(0.95),
            "last_wait_ms": self._last_wait_ms,
        }
