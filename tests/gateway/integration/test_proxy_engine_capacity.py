import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_engine_retries_real_proxy_manager_after_busy_lease_and_releases_provider():
    from autobuild_json.gateway.engine import Engine
    from autobuild_json.gateway.proxy.config import ProxySelection
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.manager import ProxyManager, endpoint_resource
    from autobuild_json.models import ProxyConfig

    events = []
    released = asyncio.Event()
    proxy = ProxyConfig("http://proxy.invalid:80")

    class DeterministicStore(MemoryLeaseStore):
        async def claim(self, resource, owner, deadline):
            token = await super().claim(resource, owner, deadline)
            if token is None and resource == endpoint_resource(proxy):
                released.set()
            return token

    store = DeterministicStore()
    manager = ProxyManager(store, acquisition_timeout=.2, heartbeat_interval=.01)
    selection = ProxySelection("fixed", runtime_entries=(proxy,))
    held = await store.claim(endpoint_resource(proxy), uuid4(), datetime.now(timezone.utc) + timedelta(seconds=2))

    async def release_holder():
        await released.wait()
        await store.release(held)

    class ProviderAdmission:
        @asynccontextmanager
        async def acquire(self, route, deadline, *, wait=False):
            events.append("provider.enter")
            try:
                yield object()
            finally:
                events.append("provider.exit")

    class Transport:
        async def _validate(self, route, selected_proxy):
            events.append(("validate", selected_proxy))

    engine = Engine.__new__(Engine)
    engine.provider_limits = ProviderAdmission()
    engine.proxies = manager
    engine.transport = Transport()
    task = asyncio.create_task(release_holder())
    try:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=2)
        async with engine.route_resources("route", selection, uuid4(), deadline) as lease:
            assert lease.proxy == proxy
    finally:
        await task

    assert events == ["provider.enter", "provider.exit", "provider.enter",
                      ("validate", proxy), "provider.exit"]
    assert manager.snapshot()["capacity_signals"] >= 1
    assert manager.snapshot()["last_wait_ms"] > 0
