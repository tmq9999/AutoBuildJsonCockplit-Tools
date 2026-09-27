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

    class Transport:
        async def _validate(self, route, proxy): events.append(("validate", proxy))

    engine = Engine.__new__(Engine)
    engine.provider_limits = ProviderAdmission()
    engine.proxies = ProxyLeases()
    engine.transport = Transport()

    async with engine.route_resources("route", "selection", "owner", __import__("datetime").datetime.now(__import__("datetime").timezone.utc)+__import__("datetime").timedelta(seconds=1)) as lease:
        assert lease.proxy == "proxy"
    assert events == [
        ("provider.enter", True), ("proxy.enter", True), "provider.exit",
        ("provider.enter", True), ("proxy.enter", True), ("validate", "proxy"),
        "proxy.exit", "provider.exit",
    ]
