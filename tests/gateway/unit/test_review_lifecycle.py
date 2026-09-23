import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading
import time
from uuid import uuid4

import pytest


def test_anthropic_encoder_preserves_initial_tool_arguments():
    from autobuild_json.gateway.protocols.anthropic import AnthropicCodec
    from autobuild_json.gateway.contracts import InferenceEvent, ToolCall

    codec = AnthropicCodec()
    codec.encode_event(InferenceEvent(kind="started"))
    frames = codec.encode_event(
        InferenceEvent(
            kind="block_started",
            item_id="x",
            block=ToolCall(call_id="x", name="weather", arguments='{"city":"Paris"}'),
        )
    )
    wire = b"".join(frames)
    assert b"input_json_delta" in wire and b"Paris" in wire


@pytest.mark.asyncio
async def test_refresh_cancellation_waits_for_http_thread_close(monkeypatch):
    import autobuild_json.gateway.accounts.refresh as mod

    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class Transport:
        def __init__(self, *a):
            pass

        def post(self, *a, **k):
            entered.set()
            release.wait(2)
            return SimpleNamespace(status_code=200, json=lambda: {})

        def close(self):
            closed.set()

    monkeypatch.setattr(mod, "SafeTransport", Transport)
    task = asyncio.create_task(
        mod.exchange_tokens(
            {"refresh_token": "synthetic"},
            SimpleNamespace(proxy=None),
            datetime.now(timezone.utc) + timedelta(seconds=30),
        )
    )
    while not entered.is_set():
        await asyncio.sleep(0.005)
    task.cancel()
    await asyncio.sleep(0.02)
    premature = task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not premature and closed.is_set()


def test_legacy_lease_loss_cancels_live_processor():
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.legacy import LegacyProxySource
    from autobuild_json.gateway.proxy.config import ProxySelection
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.models import AttemptContext, ProxyConfig
    from autobuild_json.errors import FlowError

    class Lost(MemoryLeaseStore):
        async def renew(self, token):
            return False

    manager = ProxyManager(Lost(), heartbeat_interval=0.01)
    source = LegacyProxySource(
        ProxySelection("fixed", runtime_entries=(ProxyConfig("http://proxy.invalid:80"),)), manager
    )
    context = AttemptContext(time.monotonic() + 2, threading.Event(), lambda *a: None)

    def processor(account, proxy, context):
        for _ in range(40):
            time.sleep(0.005)
            context.check()
        return "completed"

    try:
        with pytest.raises(FlowError):
            source.execute(
                SimpleNamespace(account_job_id=str(uuid4())), source.slots[0], context, processor, 2
            )
        assert context.cancel.is_set()
    finally:
        source.close()
