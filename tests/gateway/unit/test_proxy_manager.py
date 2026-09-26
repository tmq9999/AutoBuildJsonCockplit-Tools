import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

from .test_kiot import success


def deadline():
    return datetime.now(timezone.utc)+timedelta(seconds=60)


@pytest.mark.asyncio
async def test_kiot_reuses_current_and_keeps_it_for_whole_operation():
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.config import ProxySelection, parse_kiot_keys
    from autobuild_json.gateway.proxy.kiot import KiotClient
    calls = []
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=success())
    manager = ProxyManager(MemoryLeaseStore(), KiotClient(adapter=httpx.MockTransport(respond)))
    selection = ProxySelection("kiotproxy", runtime_entries=tuple(parse_kiot_keys("synthetic-key", pepper=b"p"*32)))
    async with manager.acquire(selection, uuid4(), deadline()) as lease:
        assert lease.proxy.server == "http://93.184.216.34:39008"
        await asyncio.sleep(0)
        assert lease.proxy.server == "http://93.184.216.34:39008"
    assert calls == ["/api/v1/proxies/current"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_status", [200, 400])
async def test_kiot_allocation_timeout_reconciles_current_without_second_new(missing_status):
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.config import ProxySelection, parse_kiot_keys
    from autobuild_json.gateway.proxy.kiot import KiotClient
    calls = []
    def respond(request):
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if calls == ["current"]:
            return httpx.Response(missing_status, json={"success": False, "error": "PROXY_NOT_FOUND_BY_KEY"})
        if calls[-1] == "new":
            raise httpx.ReadTimeout("contains-key", request=request)
        return httpx.Response(200, json=success())
    manager = ProxyManager(MemoryLeaseStore(), KiotClient(adapter=httpx.MockTransport(respond)))
    selection = ProxySelection("kiotproxy", runtime_entries=tuple(parse_kiot_keys("synthetic-key", pepper=b"p"*32)))
    async with manager.acquire(selection, uuid4(), deadline()) as lease:
        assert lease.proxy is not None
    assert calls == ["current", "new", "current"]


@pytest.mark.asyncio
async def test_busy_shared_key_and_explicit_release_cannot_interrupt_holder():
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.config import ProxySelection, parse_kiot_keys
    from autobuild_json.gateway.proxy.kiot import KiotClient
    from autobuild_json.gateway.errors import GatewayError
    calls = []
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=success())
    store = MemoryLeaseStore()
    kiot = KiotClient(adapter=httpx.MockTransport(respond))
    a, b = ProxyManager(store, kiot), ProxyManager(store, kiot, acquisition_timeout=0.03)
    keys = tuple(parse_kiot_keys("synthetic-key", pepper=b"p"*32))
    selection = ProxySelection("kiotproxy", runtime_entries=keys)
    async with a.acquire(selection, uuid4(), deadline()):
        with pytest.raises(GatewayError, match="proxy_not_ready"):
            async with b.acquire(selection, uuid4(), deadline()):
                pass
        with pytest.raises(GatewayError, match="proxy_not_ready"):
            await b.release_kiot(keys[0], "admin")
    assert calls == ["/api/v1/proxies/current"]


@pytest.mark.asyncio
async def test_duplicate_endpoint_serializes_different_keys_and_cancel_releases_slot():
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.config import ProxySelection, parse_kiot_keys
    from autobuild_json.gateway.proxy.kiot import KiotClient
    from autobuild_json.gateway.errors import GatewayError
    store = MemoryLeaseStore()
    manager = ProxyManager(store, KiotClient(adapter=httpx.MockTransport(lambda r: httpx.Response(200, json=success()))),
                           acquisition_timeout=0.03)
    keys = tuple(parse_kiot_keys("synthetic-A\nsynthetic-B", pepper=b"p"*32))
    async with manager.acquire(ProxySelection("kiotproxy", runtime_entries=keys[:1]), uuid4(), deadline()):
        with pytest.raises(GatewayError, match="proxy_not_ready"):
            async with manager.acquire(ProxySelection("kiotproxy", runtime_entries=keys[1:]), uuid4(), deadline()):
                pass
    acquired = asyncio.Event()
    async def holder():
        async with manager.acquire(ProxySelection("kiotproxy", runtime_entries=keys[:1]), uuid4(), deadline()):
            acquired.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(holder())
    await acquired.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with manager.acquire(ProxySelection("kiotproxy", runtime_entries=keys[:1]), uuid4(), deadline()) as lease:
        assert lease.proxy is not None


@pytest.mark.asyncio
async def test_short_cooldown_is_waited_then_allocated_once():
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.config import ProxySelection, parse_kiot_keys
    from autobuild_json.gateway.proxy.kiot import KiotClient
    calls = []
    def respond(request):
        action = request.url.path.rsplit("/", 1)[-1]
        calls.append(action)
        payload = success()
        if action == "current":
            payload["data"].update(ttl=1, ttc=0.01, expirationAt=payload["timestamp"]+1000,
                                   nextRequestAt=payload["timestamp"]+10)
        return httpx.Response(200, json=payload)
    manager = ProxyManager(MemoryLeaseStore(), KiotClient(adapter=httpx.MockTransport(respond)))
    keys = tuple(parse_kiot_keys("synthetic", pepper=b"p"*32))
    async with manager.acquire(ProxySelection("kiotproxy", runtime_entries=keys), uuid4(), deadline()) as lease:
        assert lease.proxy is not None
    assert calls == ["current", "new"]


@pytest.mark.asyncio
async def test_lease_loss_aborts_holder():
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.config import ProxySelection
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.models import ProxyConfig
    class Lost(MemoryLeaseStore):
        async def renew(self, token):
            return False
    manager = ProxyManager(Lost(), heartbeat_interval=0.01)
    with pytest.raises(GatewayError, match="proxy_error"):
        async with manager.acquire(ProxySelection("fixed", runtime_entries=(ProxyConfig("http://proxy.invalid:80"),)),
                                   uuid4(), deadline()):
            await asyncio.sleep(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_cleanup_timeout_or_cancel_joins_release_task(monkeypatch, cancel):
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.config import ProxySelection
    from autobuild_json.models import ProxyConfig
    entered, closed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class BlockedRelease(MemoryLeaseStore):
        async def release(self, token):
            entered.set()
            try:
                await release.wait()
            finally:
                closed.set()
    original_wait_for = asyncio.wait_for
    async def short_cleanup(awaitable, timeout):
        return await original_wait_for(awaitable, .02 if timeout == 5 else timeout)
    monkeypatch.setattr(asyncio, "wait_for", short_cleanup)
    manager = ProxyManager(BlockedRelease())
    async def holder():
        async with manager.acquire(ProxySelection("fixed", runtime_entries=(ProxyConfig("http://proxy.invalid:80"),)),
                                   uuid4(), deadline()):
            pass
    task = asyncio.create_task(holder())
    await entered.wait()
    if cancel:
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    was_closed = closed.is_set()
    release.set()
    await asyncio.sleep(0)
    assert was_closed, "release task survived its owner's cleanup boundary"


@pytest.mark.asyncio
async def test_cleanup_attempts_every_lease_after_one_release_fails():
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore

    class PartiallyBrokenStore(MemoryLeaseStore):
        async def release(self, token):
            if token.resource == "endpoint:failed":
                raise OSError("synthetic release failure")
            await super().release(token)

    store = PartiallyBrokenStore()
    manager = ProxyManager(store)
    key = await store.claim("kiot:key", uuid4(), deadline())
    endpoint = await store.claim("endpoint:failed", uuid4(), deadline())
    with pytest.raises(OSError, match="synthetic release failure"):
        await manager._cleanup([key, endpoint])
    assert await store.claim("kiot:key", uuid4(), deadline()) is not None
    assert await store.claim("endpoint:failed", uuid4(), deadline()) is None
