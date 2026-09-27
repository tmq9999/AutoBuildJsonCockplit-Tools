import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_route_resources_admits_provider_before_claiming_proxy():
    from autobuild_json.gateway.engine import Engine

    events = []

    class ProviderAdmission:
        @asynccontextmanager
        async def acquire(self, route, deadline):
            events.append("provider.enter")
            try:
                yield "provider-token"
            finally:
                events.append("provider.exit")

    class ProxyLeases:
        @asynccontextmanager
        async def acquire(self, selection, owner, deadline):
            events.append("proxy.enter")
            try:
                yield SimpleNamespace(proxy="proxy")
            finally:
                events.append("proxy.exit")

    class Transport:
        async def _validate(self, route, proxy):
            events.append(("validate", proxy))

    engine = Engine.__new__(Engine)
    engine.provider_limits = ProviderAdmission()
    engine.proxies = ProxyLeases()
    engine.transport = Transport()

    async with engine.route_resources("route", "selection", "owner", "deadline") as lease:
        events.append(("body", lease.proxy))

    assert events == [
        "provider.enter", "proxy.enter", ("validate", "proxy"),
        ("body", "proxy"), "proxy.exit", "provider.exit",
    ]


@pytest.mark.asyncio
async def test_route_resources_releases_provider_before_retrying_proxy_capacity():
    from autobuild_json.gateway.engine import Engine
    from autobuild_json.gateway.proxy.manager import ProxyCapacityError

    events = []

    class ProviderAdmission:
        def __init__(self): self.calls = 0
        @asynccontextmanager
        async def acquire(self, route, deadline, *, wait=False):
            self.calls += 1
            events.append(("provider.enter", wait))
            try:
                yield "provider-token"
            finally:
                events.append("provider.exit")

    class ProxyLeases:
        def __init__(self): self.calls = 0
        @asynccontextmanager
        async def acquire(self, selection, owner, deadline, *, wait=False):
            self.calls += 1
            events.append(("proxy.enter", wait))
            if self.calls == 1:
                raise ProxyCapacityError()
            try:
                yield SimpleNamespace(proxy="proxy")
            finally:
                events.append("proxy.exit")

        async def wait_for_capacity(self, selection, owner, deadline):
            events.append("proxy.wait")

    class Transport:
        async def _validate(self, route, proxy): events.append(("validate", proxy))

    engine = Engine.__new__(Engine)
    engine.provider_limits = ProviderAdmission()
    engine.proxies = ProxyLeases()
    engine.transport = Transport()

    async with engine.route_resources("route", "selection", "owner", __import__("datetime").datetime.now(__import__("datetime").timezone.utc)+__import__("datetime").timedelta(seconds=1)) as lease:
        assert lease.proxy == "proxy"
    assert events == [
        ("provider.enter", True), ("proxy.enter", False), "provider.exit", "proxy.wait",
        ("provider.enter", True), ("proxy.enter", False), ("validate", "proxy"),
        "proxy.exit", "provider.exit",
    ]


@pytest.mark.asyncio
async def test_route_resources_maps_expired_proxy_capacity_to_deadline_exceeded():
    from datetime import datetime, timedelta, timezone
    from autobuild_json.gateway.engine import Engine
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.proxy.manager import ProxyCapacityError

    events = []

    class ProviderAdmission:
        @asynccontextmanager
        async def acquire(self, route, deadline, *, wait=False):
            events.append("provider.enter")
            try:
                await asyncio.sleep(.01)
                yield object()
            finally:
                events.append("provider.exit")

    class ProxyLeases:
        @asynccontextmanager
        async def acquire(self, selection, owner, deadline, *, wait=False):
            events.append(("proxy.enter", wait))
            raise ProxyCapacityError()
            yield  # pragma: no cover

    engine = Engine.__new__(Engine)
    engine.provider_limits = ProviderAdmission()
    engine.proxies = ProxyLeases()
    engine.transport = SimpleNamespace()
    deadline = datetime.now(timezone.utc) + timedelta(milliseconds=1)
    with pytest.raises(GatewayError) as caught:
        async with engine.route_resources("route", "selection", "owner", deadline):
            pass
    assert caught.value.code == "deadline_exceeded"
    assert events == ["provider.enter", ("proxy.enter", False), "provider.exit"]
