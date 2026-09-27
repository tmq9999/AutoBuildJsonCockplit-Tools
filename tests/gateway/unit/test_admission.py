from datetime import datetime, timedelta, timezone

import asyncio

import pytest

from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.admission import InferenceAdmission


def future(seconds=30):
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


@pytest.mark.asyncio
async def test_capacity_admits_and_release_allows_next_request():
    admission = InferenceAdmission(capacity=2)
    first = await admission.enter(future())
    order = []

    async def worker(name):
        async with admission.enter(future()):
            order.append(name)
            await asyncio.sleep(0)

    second = asyncio.create_task(worker("second"))
    await asyncio.sleep(0)
    first.release()
    await second
    assert order == ["second"]

    third = asyncio.create_task(worker("third"))
    await asyncio.sleep(0)
    assert admission.snapshot()["queued"] == 0
    assert order == ["second", "third"]
    await third


@pytest.mark.asyncio
async def test_combined_capacity_rejects_third_request():
    admission = InferenceAdmission(capacity=2)
    first = await admission.enter(future())
    second = await admission.enter(future())
    with pytest.raises(GatewayError) as caught:
        await admission.enter(future())
    assert caught.value.code == "gateway_busy"
    assert caught.value.status == 429
    assert caught.value.stage == "request"
    assert caught.value.retry_after == 1
    first.release()
    second.release()


@pytest.mark.asyncio
async def test_expired_deadline_is_rejected_immediately():
    admission = InferenceAdmission(capacity=2)
    with pytest.raises(GatewayError, match="deadline_exceeded"):
        await admission.enter(datetime.now(timezone.utc) - timedelta(seconds=1))
    snapshot = admission.snapshot()
    assert snapshot["queued"] == 0
    assert snapshot["expired"] == 1


@pytest.mark.asyncio
async def test_cancelled_context_releases_admitted_ticket():
    admission = InferenceAdmission(capacity=1)
    blocked = asyncio.Event()

    async def worker():
        async with admission.enter(future()):
            await blocked.wait()

    waiter = asyncio.create_task(worker())
    while admission.snapshot()["inflight"] != 1:
        await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert admission.snapshot()["queued"] == 0
    assert admission.snapshot()["inflight"] == 0


@pytest.mark.asyncio
async def test_ticket_release_is_idempotent():
    admission = InferenceAdmission(capacity=1)
    ticket = await admission.enter(future())
    ticket.release()
    ticket.release()
    assert admission.snapshot()["inflight"] == 0
    next_ticket = await admission.enter(future())
    next_ticket.release()


def test_ticket_release_after_event_loop_shutdown_is_safe():
    admission = InferenceAdmission(capacity=1)
    ticket = asyncio.run(admission.enter(future()))
    ticket.release()
    ticket.release()
    assert admission.snapshot()["inflight"] == 0


@pytest.mark.asyncio
async def test_snapshot_tracks_counters_and_wait_percentiles():
    admission = InferenceAdmission(capacity=1)
    ticket = await admission.enter(future())
    ticket.release()
    snapshot = admission.snapshot()
    assert snapshot["capacity"] == 1
    assert snapshot["inflight"] == 0
    assert snapshot["queued"] == 0
    assert snapshot["accepted"] == 1
    assert snapshot["queue_full"] == 0
    assert snapshot["expired"] == 0
    assert snapshot["cancelled"] == 0
    assert snapshot["wait_p50_ms"] >= 0
    assert snapshot["wait_p95_ms"] >= snapshot["wait_p50_ms"]
    assert snapshot["last_wait_ms"] == snapshot["wait_p50_ms"]


@pytest.mark.asyncio
async def test_wait_history_is_bounded():
    admission = InferenceAdmission(capacity=1)
    for _ in range(2100):
        ticket = await admission.enter(future())
        ticket.release()
    assert len(admission._waits) == 2048
    assert admission.snapshot()["accepted"] == 2100
