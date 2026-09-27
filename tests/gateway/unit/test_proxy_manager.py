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
        with pytest.raises(GatewayError) as caught:
            async with b.acquire(selection, uuid4(), deadline()):
                pass
        assert caught.value.code == "proxy_capacity"
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
        with pytest.raises(GatewayError) as caught:
            async with manager.acquire(ProxySelection("kiotproxy", runtime_entries=keys[1:]), uuid4(), deadline()):
                pass
        assert caught.value.code == "proxy_capacity"
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


@pytest.mark.asyncio
async def test_wait_true_reports_retryable_capacity_after_bounded_wait():
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.config import ProxySelection
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.models import ProxyConfig

    store = MemoryLeaseStore()
    manager = ProxyManager(store, acquisition_timeout=.02)
    selection = ProxySelection("fixed", runtime_entries=(ProxyConfig("http://proxy.invalid:80"),))
    async with manager.acquire(selection, uuid4(), deadline()):
        with pytest.raises(GatewayError) as caught:
            async with manager.acquire(selection, uuid4(), deadline(), wait=True):
                pass
    assert caught.value.code == "proxy_capacity"


@pytest.mark.asyncio
async def test_capacity_wait_records_one_total_episode_after_delayed_release():
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.config import ProxySelection
    from autobuild_json.models import ProxyConfig

    store = MemoryLeaseStore()
    manager = ProxyManager(store, acquisition_timeout=.2)
    selection = ProxySelection("fixed", runtime_entries=(ProxyConfig("http://proxy.invalid:80"),))
    held = await store.claim("endpoint:" + __import__("hashlib").sha256(
        "http://proxy.invalid:80".encode()).hexdigest(), uuid4(), deadline())
    release = asyncio.create_task(asyncio.sleep(.05))
    async def release_later():
        await release
        await store.release(held)
    asyncio.create_task(release_later())
    await manager.wait_for_capacity(selection, uuid4(), deadline())
    snapshot = manager.snapshot()
    assert snapshot["wait_p50_ms"] >= 40
    assert snapshot["wait_attempts"] >= 1


@pytest.mark.asyncio
async def test_wait_false_makes_one_claim_without_sleeping():
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.config import ProxySelection
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.models import ProxyConfig

    class CountingStore(MemoryLeaseStore):
        def __init__(self):
            super().__init__()
            self.claims = 0

        async def claim(self, *args, **kwargs):
            self.claims += 1
            return await super().claim(*args, **kwargs)

    store = CountingStore()
    manager = ProxyManager(store, acquisition_timeout=1)
    selection = ProxySelection("fixed", runtime_entries=(ProxyConfig("http://proxy.invalid:80"),))
    async with manager.acquire(selection, uuid4(), deadline()):
        started = asyncio.get_running_loop().time()
        with pytest.raises(GatewayError) as caught:
            async with manager.acquire(selection, uuid4(), deadline(), wait=False):
                pass
        assert caught.value.code == "proxy_capacity"
        assert str(caught.value) == "proxy_not_ready"
        assert store.claims == 2
        assert asyncio.get_running_loop().time() - started < 0.1


@pytest.mark.asyncio
async def test_disabled_endpoint_is_permanent_not_capacity_signal():
    from autobuild_json.gateway.proxy.manager import ProxyManager, endpoint_resource
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.config import ProxySelection
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.models import ProxyConfig

    store = MemoryLeaseStore()
    proxy = ProxyConfig("http://proxy.invalid:80")
    await store.disable_resource(endpoint_resource(proxy))
    manager = ProxyManager(store, acquisition_timeout=.02)
    selection = ProxySelection("fixed", runtime_entries=(proxy,))
    with pytest.raises(GatewayError) as caught:
        async with manager.acquire(selection, uuid4(), deadline(), wait=False):
            pass
    assert caught.value.code == "proxy_not_ready"
    assert manager.snapshot()["capacity_signals"] == 0


@pytest.mark.asyncio
async def test_wait_true_cancellation_releases_partial_kiot_claims():
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.config import ProxySelection, parse_kiot_keys
    from autobuild_json.gateway.proxy.kiot import KiotClient

    store = MemoryLeaseStore()
    manager = ProxyManager(store, KiotClient(adapter=httpx.MockTransport(lambda r: httpx.Response(200, json=success()))))
    keys = tuple(parse_kiot_keys("synthetic-A\nsynthetic-B", pepper=b"p"*32))
    selection = ProxySelection("kiotproxy", runtime_entries=keys)
    held = await store.claim("endpoint:" + __import__("hashlib").sha256("http://93.184.216.34:39008".encode()).hexdigest(), uuid4(), deadline())
    task = asyncio.create_task(manager.acquire(selection, uuid4(), deadline(), wait=True).__aenter__())
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await store.claim("kiot:" + keys[0].fingerprint, uuid4(), deadline()) is not None
    await store.release(held)


@pytest.mark.asyncio
async def test_kiot_proxy_not_ready_is_permanent_even_when_waiting():
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.config import ProxySelection, parse_kiot_keys
    from autobuild_json.gateway.errors import GatewayError

    class Kiot:
        async def current(self, *args, **kwargs):
            raise GatewayError("proxy_not_ready", 503, "proxy")

    manager = ProxyManager(MemoryLeaseStore(), Kiot(), acquisition_timeout=.2)
    selection = ProxySelection("kiotproxy", runtime_entries=tuple(parse_kiot_keys("key", pepper=b"p"*32)))
    with pytest.raises(GatewayError) as caught:
        async with manager.acquire(selection, uuid4(), deadline(), wait=True):
            pass
    assert caught.value.code == "proxy_not_ready"


@pytest.mark.asyncio
async def test_mixed_invalid_key_and_busy_valid_key_reports_capacity():
    from autobuild_json.gateway.proxy.manager import ProxyManager, endpoint_resource
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.config import ProxySelection, parse_kiot_keys
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.models import ProxyConfig

    class Kiot:
        async def current(self, secret, **kwargs):
            if secret == "bad":
                raise GatewayError("kiot_key_invalid", 400, "proxy")
            return type("Lease", (), {"ttl": 1000, "cooldown": 0, "proxy": ProxyConfig("http://proxy.invalid:80")})()

    store = MemoryLeaseStore()
    manager = ProxyManager(store, Kiot(), acquisition_timeout=.02)
    keys = tuple(parse_kiot_keys("bad\ngood", pepper=b"p"*32))
    held = await store.claim(endpoint_resource(ProxyConfig("http://proxy.invalid:80")), uuid4(), deadline())
    with pytest.raises(Exception) as caught:
        async with manager.acquire(ProxySelection("kiotproxy", runtime_entries=keys), uuid4(), deadline(), wait=True):
            pass
    assert caught.value.code == "proxy_capacity"
    await store.release(held)


@pytest.mark.asyncio
async def test_cleanup_with_acquisition_deadline_cancels_a_blocked_release():
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore

    entered, closed = asyncio.Event(), asyncio.Event()

    class BlockedRelease(MemoryLeaseStore):
        async def release(self, token):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

    store = BlockedRelease()
    manager = ProxyManager(store)
    token = await store.claim("endpoint:blocked", uuid4(), deadline())
    end = asyncio.get_running_loop().time() + 0.02
    task = asyncio.create_task(manager._cleanup([token], end))
    await entered.wait()
    with pytest.raises(Exception):
        await task
    assert closed.is_set()


@pytest.mark.asyncio
async def test_cleanup_owned_retains_token_when_release_budget_expires():
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore

    class BlockedRelease(MemoryLeaseStore):
        async def release(self, token):
            await asyncio.Event().wait()

    store = BlockedRelease()
    manager = ProxyManager(store)
    token = await store.claim("endpoint:retained", uuid4(), deadline())
    tokens = [token]
    with pytest.raises(Exception):
        await manager._cleanup_owned(tokens, asyncio.get_running_loop().time() + 0.01)
    assert tokens == [token]


@pytest.mark.asyncio
async def test_invalid_kiot_key_passes_acquisition_deadline_to_health_disable():
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.config import ProxySelection, parse_kiot_keys
    from autobuild_json.gateway.errors import GatewayError

    class HealthStore(MemoryLeaseStore):
        def __init__(self):
            super().__init__()
            self.deadline = None

        async def disable_resource(self, resource, deadline=None):
            self.deadline = deadline
            await super().disable_resource(resource, deadline)

    class InvalidKiot:
        async def current(self, *args, **kwargs):
            raise GatewayError("kiot_key_invalid", 400, "proxy")

    store = HealthStore()
    manager = ProxyManager(store, InvalidKiot(), acquisition_timeout=.1)
    selection = ProxySelection("kiotproxy", runtime_entries=tuple(parse_kiot_keys("bad", pepper=b"p" * 32)))
    with pytest.raises(GatewayError, match="kiot_key_invalid"):
        async with manager.acquire(selection, uuid4(), deadline()):
            pass
    assert isinstance(store.deadline, datetime)
    assert store.deadline < datetime.now(timezone.utc) + timedelta(seconds=.2)


@pytest.mark.asyncio
async def test_release_kiot_retries_transient_lease_release_failure():
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from types import SimpleNamespace

    class FlakyStore(MemoryLeaseStore):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def release(self, token):
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary release failure")
            await super().release(token)

    class Kiot:
        async def out(self, *args, **kwargs):
            return None

    store = FlakyStore()
    manager = ProxyManager(store, Kiot())
    key = SimpleNamespace(fingerprint="key", secret="secret")
    await manager.release_kiot(key, "admin")
    assert store.calls == 2
    assert await store.claim("kiot:key", uuid4(), deadline()) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["proxy_not_ready", "kiot_unavailable", "kiot_key_invalid"])
async def test_kiot_failure_at_acquisition_expiry_is_not_retried_as_capacity(code):
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.config import ProxySelection, parse_kiot_keys
    from autobuild_json.gateway.errors import GatewayError

    class KiotFailure:
        async def current(self, *args, **kwargs):
            await asyncio.sleep(.03)
            raise GatewayError(code, 503, "proxy")

    store = MemoryLeaseStore()
    manager = ProxyManager(store, KiotFailure(), acquisition_timeout=.02)
    key = tuple(parse_kiot_keys("bad", pepper=b"p"*32))
    with pytest.raises(GatewayError) as caught:
        async with manager.acquire(ProxySelection("kiotproxy", runtime_entries=key), uuid4(), deadline()):
            pass
    assert caught.value.code == code
    assert await store.claim("kiot:" + key[0].fingerprint, uuid4(), deadline()) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("slow", [False, True])
async def test_persistent_release_failure_is_short_bounded_and_preserves_kiot_error(slow):
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.config import ProxySelection, parse_kiot_keys
    from autobuild_json.gateway.errors import GatewayError

    class BrokenStore(MemoryLeaseStore):
        calls = 0

        async def release(self, token):
            self.calls += 1
            if slow:
                await asyncio.sleep(.25)
            raise GatewayError("deadline_exceeded", 504, "proxy")

    class KiotFailure:
        async def current(self, *args, **kwargs):
            await asyncio.sleep(.03)
            raise GatewayError("kiot_unavailable", 503, "proxy")

    store = BrokenStore()
    manager = ProxyManager(store, KiotFailure(), acquisition_timeout=.02)
    keys = tuple(parse_kiot_keys("unavailable", pepper=b"p"*32))
    started = asyncio.get_running_loop().time()
    with pytest.raises(GatewayError) as caught:
        async with manager.acquire(ProxySelection("kiotproxy", runtime_entries=keys), uuid4(), deadline()):
            pass
    assert caught.value.code == "kiot_unavailable"
    assert store.calls <= 2
    assert asyncio.get_running_loop().time() - started < .9
