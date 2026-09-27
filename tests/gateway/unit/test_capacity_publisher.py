import asyncio
import time

import pytest

from autobuild_json.gateway.admission import InferenceAdmission
from autobuild_json.gateway.engine import Engine


@pytest.mark.asyncio
async def test_capacity_publisher_coalesces_notifications_and_joins_on_shutdown():
    engine = Engine.__new__(Engine)
    engine._capacity_interval = 0.01
    writes = []

    async def publish():
        writes.append(1)

    engine._publish_capacity = publish
    await engine.start_capacity_publisher()
    await asyncio.sleep(0.02)  # discard the lifecycle's initial snapshot
    writes.clear()
    for _ in range(100):
        engine.request_capacity_publish()
    await asyncio.sleep(0.03)
    assert len(writes) == 1
    await engine.stop_capacity_publisher()

    assert engine._capacity_task is None
    assert not [task for task in asyncio.all_tasks() if task.get_name() == "gateway-capacity-publisher"]


def test_wait_sample_memory_is_bounded():
    admission = InferenceAdmission(1)
    for value in range(5000):
        admission._waits.append(value)
    snapshot = admission.snapshot()

    assert len(admission._waits) == 2048
    assert snapshot["wait_p95_ms"] <= 4999


@pytest.mark.asyncio
async def test_stop_reports_cancellation_resistant_publisher_without_detaching_it():
    engine = Engine.__new__(Engine)
    engine._capacity_interval = 0
    release = asyncio.Event()

    async def resistant_worker():
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Simulate a writer that cannot immediately honor cancellation.
            await release.wait()

    engine._capacity_publisher = resistant_worker
    await engine.start_capacity_publisher()
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await engine.stop_capacity_publisher(timeout=0.01)
    assert time.monotonic() - started < 0.2
    assert engine._capacity_task is not None and not engine._capacity_task.done()

    release.set()
    engine._capacity_task.cancel()
    await asyncio.gather(engine._capacity_task, return_exceptions=True)
