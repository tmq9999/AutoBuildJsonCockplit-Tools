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
