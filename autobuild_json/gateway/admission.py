"""Process-local bounded admission for inference requests."""

import asyncio
from collections import deque
from datetime import datetime, timezone
from math import ceil
from time import monotonic

from .errors import GatewayError


class _ResourceWait:
    def __init__(self, admission, resource):
        self.admission = admission
        self.resource = resource
        self.started = monotonic()
        self.ended = False


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
        # Keep telemetry bounded even for a long-lived gateway process.  The
        # gate is an accepted-counter (not an unbounded FIFO), so only the
        # recent wait sample is retained for percentiles.
        self._waits = deque(maxlen=2048)
        self._last_wait_ms = 0.0
        self._resource_waits = deque(maxlen=2048)
        self._resource_waiting = 0
        self._resource_waiting_by_type = {"provider": 0, "proxy": 0}
        self._resource_wait_episodes = 0
        self._resource_wait_expired = 0
        self._resource_wait_cancelled = 0
        self._resource_wait_success = 0
        self._resource_wait_failed = 0
        self._last_resource_wait_ms = 0.0
        self._stage_outcomes = {
            stage: {outcome: 0 for outcome in ("expired", "cancelled", "rate_limited", "failed")}
            for stage in ("provider", "proxy", "upstream")
        }

    def enter(self, deadline):
        return _AdmissionContext(self, deadline)

    def begin_resource_wait(self, resource):
        self._resource_waiting += 1
        self._resource_waiting_by_type[resource] = self._resource_waiting_by_type.get(resource, 0) + 1
        self._emit_metrics()
        return _ResourceWait(self, resource)

    def end_resource_wait(self, episode, outcome="success"):
        if episode is None or episode.ended:
            return
        episode.ended = True
        self._resource_waiting = max(0, self._resource_waiting - 1)
        self._resource_waiting_by_type[episode.resource] = max(
            0, self._resource_waiting_by_type.get(episode.resource, 0) - 1)
        elapsed = (monotonic() - episode.started) * 1000
        self._resource_waits.append(elapsed)
        self._last_resource_wait_ms = elapsed
        self._resource_wait_episodes += 1
        if outcome == "expired":
            self._resource_wait_expired += 1
        elif outcome == "cancelled":
            self._resource_wait_cancelled += 1
        elif outcome == "failed":
            self._resource_wait_failed += 1
        else:
            self._resource_wait_success += 1
        self._emit_metrics()

    def record_stage_outcome(self, stage, outcome):
        """Record a redacted terminal outcome for a bounded wait stage."""
        bucket = self._stage_outcomes.get(stage)
        if bucket is None or outcome not in bucket:
            return
        bucket[outcome] += 1
        self._emit_metrics()

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
        resource_waits = sorted(self._resource_waits)

        def percentile(percent):
            if not waits:
                return 0.0
            return waits[max(0, ceil(len(waits) * percent) - 1)]

        def resource_percentile(percent):
            if not resource_waits:
                return 0.0
            return resource_waits[max(0, ceil(len(resource_waits) * percent) - 1)]

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
            "queue_wait_p50_ms": percentile(0.50),
            "queue_wait_p95_ms": percentile(0.95),
            "queue_wait_expired": self._expired,
            "queue_wait_cancelled": self._cancelled,
            "resource_waiting": self._resource_waiting,
            "provider_waiting": self._resource_waiting_by_type.get("provider", 0),
            "proxy_waiting": self._resource_waiting_by_type.get("proxy", 0),
            "resource_wait_episodes": self._resource_wait_episodes,
            "resource_wait_success": self._resource_wait_success,
            "resource_wait_failed": self._resource_wait_failed,
            "resource_wait_expired": self._resource_wait_expired,
            "resource_wait_cancelled": self._resource_wait_cancelled,
            "resource_wait_p50_ms": resource_percentile(.50),
            "resource_wait_p95_ms": resource_percentile(.95),
            "last_resource_wait_ms": self._last_resource_wait_ms,
            "deadline_expired_by_stage": {
                stage: values["expired"] for stage, values in self._stage_outcomes.items()
            },
            "cancelled_by_stage": {
                stage: values["cancelled"] for stage, values in self._stage_outcomes.items()
            },
            "rate_limited_by_stage": {
                stage: values["rate_limited"] for stage, values in self._stage_outcomes.items()
            },
            "failed_by_stage": {
                stage: values["failed"] for stage, values in self._stage_outcomes.items()
            },
        }
