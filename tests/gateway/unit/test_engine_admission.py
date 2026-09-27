from autobuild_json.gateway.admission import InferenceAdmission
from autobuild_json.gateway.engine import Engine
from autobuild_json.gateway.settings import ServiceSettings
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


def test_service_settings_exposes_inference_capacity():
    assert ServiceSettings(_env_file=None).inference_capacity == 100


def test_direct_engine_uses_capacity_100_admission():
    engine = Engine.__new__(Engine)
    # Constructor wiring is exercised without requiring a database harness.
    Engine.__init__(engine, None, None, None, None, None, None)
    assert isinstance(engine.admission, InferenceAdmission)
    assert engine.admission.capacity == 100


@pytest.mark.asyncio
async def test_prepared_call_holds_ticket_until_stream_close():
    admission = InferenceAdmission(1)
    admission_stack = AsyncExitStack()
    await admission_stack.enter_async_context(admission.enter(datetime.now(timezone.utc) + timedelta(seconds=2)))

    class Stream:
        events = ()
        cancelled = False

        async def cancel(self):
            self.cancelled = True

    class Ledger:
        async def mark_pending(self, *args):
            pass

    from autobuild_json.gateway.engine import PreparedCall
    prepared = PreparedCall(SimpleNamespace(ledger=Ledger(), budgets=None), AsyncExitStack(), Stream(),
                            __import__("uuid").uuid4(), SimpleNamespace(public_model_id="m", adapter="x"),
                            __import__("uuid").uuid4(), admission_stack=admission_stack)
    assert admission.snapshot()["inflight"] == 1
    await prepared.close()
    await asyncio.sleep(0)
    assert admission.snapshot()["inflight"] == 0
    assert prepared.closed
