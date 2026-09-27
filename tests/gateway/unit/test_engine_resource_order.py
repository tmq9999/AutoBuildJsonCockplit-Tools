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


@pytest.mark.asyncio
async def test_route_resources_publishes_resource_wait_transitions():
    from datetime import datetime, timedelta, timezone
    from autobuild_json.gateway.engine import Engine

    events = []

    class Admission:
        def begin_resource_wait(self, resource):
            events.append(("begin", resource))
            return resource

        def end_resource_wait(self, episode, outcome):
            events.append(("end", episode, outcome))

    class ProviderAdmission:
        @asynccontextmanager
        async def acquire(self, route, deadline, *, wait=False):
            yield object()

    class ProxyLeases:
        @asynccontextmanager
        async def acquire(self, selection, owner, deadline, *, wait=False):
            yield SimpleNamespace(proxy="proxy")

    class Transport:
        async def _validate(self, route, proxy):
            return None

    engine = Engine.__new__(Engine)
    engine.admission = Admission()
    engine.provider_limits = ProviderAdmission()
    engine.proxies = ProxyLeases()
    engine.transport = Transport()
    engine.request_capacity_publish = lambda: events.append("publish")

    selection = SimpleNamespace(mode="fixed")
    async with engine.route_resources(
        "route", selection, "owner", datetime.now(timezone.utc) + timedelta(seconds=1)
    ):
        pass

    assert events == [
        ("begin", "provider"), "publish",
        ("end", "provider", "success"), "publish",
        ("begin", "proxy"), "publish",
        ("end", "proxy", "success"), "publish",
    ]


@pytest.mark.asyncio
async def test_route_resources_does_not_report_proxy_wait_for_direct_selection():
    from datetime import datetime, timedelta, timezone
    from autobuild_json.gateway.engine import Engine

    events = []

    class Admission:
        def begin_resource_wait(self, resource):
            events.append(("begin", resource))
            return resource

        def end_resource_wait(self, episode, outcome):
            events.append(("end", episode, outcome))

    class ProviderAdmission:
        @asynccontextmanager
        async def acquire(self, route, deadline, *, wait=False):
            yield object()

    class ProxyLeases:
        @asynccontextmanager
        async def acquire(self, selection, owner, deadline, *, wait=False):
            yield SimpleNamespace(proxy=None)

    engine = Engine.__new__(Engine)
    engine.admission = Admission()
    engine.provider_limits = ProviderAdmission()
    engine.proxies = ProxyLeases()
    engine.transport = SimpleNamespace(_validate=lambda route, proxy: asyncio.sleep(0))

    async with engine.route_resources(
        "route", SimpleNamespace(mode="direct"), "owner",
        datetime.now(timezone.utc) + timedelta(seconds=1),
    ):
        pass

    assert events == [("begin", "provider"), ("end", "provider", "success")]


@pytest.mark.asyncio
async def test_route_resources_marks_unexpected_provider_and_proxy_errors_failed():
    from datetime import datetime, timedelta, timezone
    from autobuild_json.gateway.admission import InferenceAdmission
    from autobuild_json.gateway.engine import Engine

    admission = InferenceAdmission(2)

    class ProviderFailure:
        @asynccontextmanager
        async def acquire(self, route, deadline, *, wait=False):
            raise RuntimeError("provider store failure")
            yield  # pragma: no cover

    engine = Engine.__new__(Engine)
    engine.admission = admission
    engine.provider_limits = ProviderFailure()
    engine.proxies = SimpleNamespace(acquire=engine.provider_limits.acquire)
    with pytest.raises(RuntimeError):
        async with engine.route_resources(
            "route", SimpleNamespace(mode="fixed"), "owner",
            datetime.now(timezone.utc) + timedelta(seconds=1),
        ):
            pass
    assert admission.snapshot()["resource_wait_failed"] == 1

    class ProviderSuccess:
        @asynccontextmanager
        async def acquire(self, route, deadline, *, wait=False):
            yield object()

    class ProxyFailure:
        @asynccontextmanager
        async def acquire(self, selection, owner, deadline, *, wait=False):
            raise RuntimeError("proxy store failure")
            yield  # pragma: no cover

    engine.provider_limits = ProviderSuccess()
    engine.proxies = ProxyFailure()
    with pytest.raises(RuntimeError):
        async with engine.route_resources(
            "route", SimpleNamespace(mode="fixed"), "owner",
            datetime.now(timezone.utc) + timedelta(seconds=1),
        ):
            pass
    assert admission.snapshot()["resource_wait_failed"] == 2
