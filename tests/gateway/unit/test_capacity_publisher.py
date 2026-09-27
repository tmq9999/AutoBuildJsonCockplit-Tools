import asyncio

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
