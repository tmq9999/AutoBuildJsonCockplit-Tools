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


@pytest.mark.asyncio
async def test_prepared_cleanup_releases_admission_when_resource_cleanup_fails():
    admission = InferenceAdmission(1)
    admission_stack = AsyncExitStack()
    await admission_stack.enter_async_context(admission.enter(datetime.now(timezone.utc) + timedelta(seconds=2)))

    class BrokenStack:
        async def aclose(self):
            raise RuntimeError("resource cleanup failed")

    class Stream:
        async def cancel(self):
            pass

    from autobuild_json.gateway.engine import PreparedCall
    prepared = PreparedCall(SimpleNamespace(ledger=SimpleNamespace(mark_pending=lambda *a: _async_value(None)), budgets=None),
                            BrokenStack(), Stream(), __import__("uuid").uuid4(),
                            SimpleNamespace(public_model_id="m", adapter="x"), __import__("uuid").uuid4(),
                            admission_stack=admission_stack)
    with pytest.raises(RuntimeError, match="resource cleanup failed"):
        await prepared.close()
    assert admission.snapshot()["inflight"] == 0


@pytest.mark.asyncio
async def test_prepared_close_joins_one_recovery_cleanup_and_settles_saved_usage():
    admission = InferenceAdmission(1)
    admission_stack = AsyncExitStack()
    await admission_stack.enter_async_context(admission.enter(datetime.now(timezone.utc) + timedelta(seconds=2)))
    calls = []

    class Ledger:
        async def reconcile_interrupted(self, *args):
            calls.append(args)

    class Stream:
        async def cancel(self):
            pass

    from autobuild_json.gateway.engine import PreparedCall
    request_id, attempt_id = __import__("uuid").uuid4(), __import__("uuid").uuid4()
    prepared = PreparedCall(SimpleNamespace(ledger=Ledger(), budgets=None), AsyncExitStack(), Stream(), request_id,
                            SimpleNamespace(public_model_id="m", adapter="x"), attempt_id,
                            admission_stack=admission_stack)
    await asyncio.gather(prepared.close(), prepared.close())
    assert len(calls) == 1 and calls[0] == (request_id, attempt_id)
    assert admission.snapshot()["inflight"] == 0
    assert prepared.closed


@pytest.mark.asyncio
async def test_prepared_close_bounds_hanging_cleanup_and_does_not_claim_closed():
    admission = InferenceAdmission(1)
    admission_stack = AsyncExitStack()
    await admission_stack.enter_async_context(admission.enter(datetime.now(timezone.utc) + timedelta(seconds=2)))
    entered, release = asyncio.Event(), asyncio.Event()

    class Ledger:
        async def mark_pending(self, *args):
            entered.set()
            await release.wait()

    class Stream:
        async def cancel(self):
            pass

    from autobuild_json.gateway.engine import PreparedCall
    prepared = PreparedCall(SimpleNamespace(ledger=Ledger(), budgets=None), AsyncExitStack(), Stream(),
                            __import__("uuid").uuid4(), SimpleNamespace(public_model_id="m", adapter="x"),
                            __import__("uuid").uuid4(), admission_stack=admission_stack,
                            cleanup_grace=0.05)
    started = asyncio.get_running_loop().time()
    outcomes = await asyncio.gather(prepared.close(), prepared.close(), return_exceptions=True)
    assert all(isinstance(outcome, TimeoutError) for outcome in outcomes)
    assert await asyncio.wait_for(entered.wait(), 0.1)
    assert asyncio.get_running_loop().time() - started < 0.5
    assert not prepared.closed
    assert admission.snapshot()["inflight"] == 0
    assert prepared._cleanup_children
    release.set()
    with pytest.raises(TimeoutError):
        await prepared.close()
    assert not prepared._cleanup_children


@pytest.mark.asyncio
async def test_prepared_close_retains_and_later_joins_cancellation_resistant_cleanup():
    admission = InferenceAdmission(1)
    admission_stack = AsyncExitStack()
    await admission_stack.enter_async_context(admission.enter(datetime.now(timezone.utc) + timedelta(seconds=2)))
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Ledger:
        async def mark_pending(self, *args):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finally:
                finished.set()

    class Stream:
        async def cancel(self):
            pass

    from autobuild_json.gateway.engine import PreparedCall
    prepared = PreparedCall(SimpleNamespace(ledger=Ledger(), budgets=None), AsyncExitStack(), Stream(),
                            __import__("uuid").uuid4(), SimpleNamespace(public_model_id="m", adapter="x"),
                            __import__("uuid").uuid4(), admission_stack=admission_stack,
                            cleanup_grace=0.05)
    with pytest.raises(TimeoutError):
        await prepared.close()
    assert entered.is_set() and not finished.is_set()
    assert prepared._cleanup_children and not prepared.closed
    release.set()
    await prepared.close()
    assert finished.is_set() and not prepared._cleanup_children
    assert prepared.closed and admission.snapshot()["inflight"] == 0


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
