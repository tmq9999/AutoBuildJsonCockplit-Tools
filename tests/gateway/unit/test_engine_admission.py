from autobuild_json.gateway.admission import InferenceAdmission
from autobuild_json.gateway.engine import Engine
from autobuild_json.gateway.settings import ServiceSettings
from autobuild_json.gateway import engine as engine_module
from autobuild_json.gateway.errors import GatewayError
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


class _Ledger:
    def __init__(self):
        self.released = []

    async def reserve(self, admission):
        return object()

    async def release_unspent(self, request_id, reason):
        self.released.append((request_id, reason))


def _prepare_engine(monkeypatch, admission):
    route = SimpleNamespace(adapter="codex_oauth", bounds=SimpleNamespace(input_tokens=1, output_tokens=1),
                            public_model_id="m", input_micro=1, output_micro=1, timeout=30,
                            idempotency_digest=None, payload_digest=None, protocol="openai",
                            cache_read_micro=0, cache_write_micro=0, provider_id="p", credential_id="c")
    state = SimpleNamespace(request=SimpleNamespace(), routes=(route,), scope=SimpleNamespace(public_model_id="m"),
                            pinned=False, retry_limit=0)
    engine = Engine.__new__(Engine)
    engine.ledger = _Ledger()
    engine.admission = admission
    engine.adapters = {"codex_oauth": object()}
    engine.credentials = SimpleNamespace(fresh_tokens=lambda *args: _async_value(None))
    engine.catalog = SimpleNamespace(visible_model=lambda *args: _async_value("m"))
    monkeypatch.setattr(engine_module.routing_session, "select", lambda *args: _async_value(state))
    monkeypatch.setattr(engine_module, "validate_request", lambda *args: None)
    return engine, route


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_prepare_deadline_releases_unspent_ledger_hold(monkeypatch):
    class Expired:
        def enter(self, deadline):
            class Context:
                async def __aenter__(self):
                    raise GatewayError("deadline_exceeded", 504, "request")
                async def __aexit__(self, *args):
                    return False
            return Context()

    engine, route = _prepare_engine(monkeypatch, Expired())
    meta = SimpleNamespace(request_id=__import__("uuid").uuid4(), deadline=datetime.now(timezone.utc) + timedelta(seconds=1),
                            idempotency_digest=None, payload_digest=None, protocol="openai")
    with pytest.raises(GatewayError):
        await engine.prepare("principal", SimpleNamespace(), meta)
    assert engine.ledger.released == [(meta.request_id, "not_dispatched")]


@pytest.mark.asyncio
async def test_prepare_cancellation_releases_admission_ticket(monkeypatch):
    admission = InferenceAdmission(1)
    engine, route = _prepare_engine(monkeypatch, admission)
    async def cancel(*args):
        raise asyncio.CancelledError
    engine.credentials.fresh_tokens = cancel
    meta = SimpleNamespace(request_id=__import__("uuid").uuid4(), deadline=datetime.now(timezone.utc) + timedelta(seconds=1),
                            idempotency_digest=None, payload_digest=None, protocol="openai")
    with pytest.raises(asyncio.CancelledError):
        await engine.prepare("principal", SimpleNamespace(), meta)
    assert admission.snapshot()["inflight"] == 0
