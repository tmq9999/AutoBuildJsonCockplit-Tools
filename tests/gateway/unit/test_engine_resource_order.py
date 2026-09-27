import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

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


@pytest.mark.asyncio
async def test_prepare_releases_attempt_resources_before_retrying_pre_response_proxy_failure(monkeypatch):
    import autobuild_json.gateway.engine as engine_module
    from autobuild_json.gateway.engine import Engine, RequestMeta
    from autobuild_json.gateway.errors import TransportFailure
    from autobuild_json.gateway.routing.session import Selection

    events = []
    route_one = SimpleNamespace(
        adapter="fake", provider_id="provider-a", credential_id="credential-a", proxy_profile_id=None,
        public_model_id="public", binding_id="binding-a", upstream_model="upstream",
        budget_id=None, bounds=SimpleNamespace(input_tokens=1, output_tokens=2), input_micro=1,
        output_micro=1, cache_read_micro=None, cache_write_micro=None, timeout=60,
    )
    route_two = SimpleNamespace(**{**route_one.__dict__, "provider_id": "provider-b"})
    options = SimpleNamespace(max_output_tokens=None, model_copy=lambda **kwargs: options)
    request = SimpleNamespace(
        options=options, model="public",
    )
    request.model_copy = lambda **kwargs: request
    scope = SimpleNamespace(public_model_id="public")
    meta = RequestMeta(uuid4(), datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(seconds=30))

    class Admission:
        @asynccontextmanager
        async def enter(self, deadline):
            yield

    class Ledger:
        async def reserve(self, admission):
            return "hold"

        async def resize(self, hold, bounds):
            return hold

        async def mark_dispatched(self, request_id, attempt, *, route):
            events.append(("dispatched", route.provider_id))

        async def mark_rejected(self, request_id, attempt, reason):
            events.append(("rejected", reason))

    class Budgets:
        async def settle(self, attempt, amount):
            pass

    class Catalog:
        async def visible_model(self, principal, model):
            return model

    class Adapter:
        def __init__(self):
            self.calls = 0

        @asynccontextmanager
        async def open(self, request, route, lease):
            self.calls += 1
            if self.calls == 1:
                raise TransportFailure(before_response=True, proxy_used=True)
            yield SimpleNamespace()

    adapter = Adapter()
    engine = Engine.__new__(Engine)
    engine.catalog = Catalog()
    engine.ledger = Ledger()
    engine.admission = Admission()
    engine.budgets = Budgets()
    engine.adapters = {"fake": adapter}
    engine.proxy_resolver = None
    engine.credential = lambda route: asyncio.sleep(0)

    @asynccontextmanager
    async def resources(route, selection, owner, deadline):
        events.append(("enter", route.provider_id))
        try:
            yield SimpleNamespace(proxy="proxy")
        finally:
            events.append(("exit", route.provider_id))

    engine.route_resources = resources
    monkeypatch.setattr(engine_module, "validate_request", lambda request, route: None)
    monkeypatch.setattr(
        engine_module.routing_session,
        "select",
        lambda engine, principal, request, meta: asyncio.sleep(0, result=Selection(
            request, (route_one, route_two), scope, False, 1)),
    )
    monkeypatch.setattr(engine_module.routing_session, "revalidate", lambda *args: asyncio.sleep(0))

    prepared = await engine.prepare(object(), request, meta)
    assert prepared.route.provider_id == "provider-b"
    assert events == [
        ("enter", "provider-a"), ("dispatched", "provider-a"), ("rejected", "rejected_before_generation"),
        ("exit", "provider-a"), ("enter", "provider-b"), ("dispatched", "provider-b"),
    ]


@pytest.mark.asyncio
async def test_prepare_retries_pre_response_transport_failure_at_most_once(monkeypatch):
    import autobuild_json.gateway.engine as engine_module
    from autobuild_json.gateway.engine import Engine, RequestMeta
    from autobuild_json.gateway.errors import GatewayError, TransportFailure
    from autobuild_json.gateway.proxy.config import ProxySelection
    from autobuild_json.gateway.routing.session import Selection

    routes = [SimpleNamespace(
        adapter="codex_oauth", provider_id="provider", credential_id=f"credential-{index}",
        proxy_profile_id=None, public_model_id="public", binding_id=f"binding-{index}",
        upstream_model="upstream", budget_id=None,
        bounds=SimpleNamespace(input_tokens=1, output_tokens=2), input_micro=1, output_micro=1,
        cache_read_micro=None, cache_write_micro=None, timeout=60,
    ) for index in range(3)]
    options = SimpleNamespace(max_output_tokens=None, model_copy=lambda **kwargs: options)
    request = SimpleNamespace(options=options, model="public")
    request.model_copy = lambda **kwargs: request
    scope = SimpleNamespace(public_model_id="public")
    meta = RequestMeta(uuid4(), datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(seconds=30))
    events = []

    class Ledger:
        async def reserve(self, admission): return "hold"
        async def resize(self, hold, bounds): return hold
        async def mark_dispatched(self, request_id, attempt, *, route): events.append(("dispatched", route.provider_id))
        async def mark_rejected(self, request_id, attempt, reason): events.append(("rejected", reason))
        async def reject_and_release(self, request_id, attempt): events.append(("released", attempt))

    class Adapter:
        def __init__(self): self.calls = 0

        @asynccontextmanager
        async def open(self, request, route, lease):
            self.calls += 1
            raise TransportFailure(before_response=True, proxy_used=True)
            yield  # pragma: no cover

    adapter = Adapter()
    engine = Engine.__new__(Engine)
    engine.catalog = SimpleNamespace(visible_model=lambda principal, model: asyncio.sleep(0, result=model))
    engine.ledger = Ledger()
    engine.admission = SimpleNamespace(enter=lambda deadline: _empty_context())
    engine.budgets = SimpleNamespace(settle=lambda *args: asyncio.sleep(0))
    engine.adapters = {"codex_oauth": adapter}
    engine.db = None
    engine.credentials = SimpleNamespace(
        fresh_tokens=lambda credential_id, deadline: asyncio.sleep(0),
        _record=lambda credential_id: asyncio.sleep(0),
    )
    engine.proxy_resolver = None
    engine.credential = lambda route: asyncio.sleep(0)

    @asynccontextmanager
    async def resources(route, selection, owner, deadline):
        events.append(("enter", route.provider_id))
        try:
            yield SimpleNamespace(proxy="proxy")
        finally:
            events.append(("exit", route.provider_id))

    @asynccontextmanager
    async def admission_context(deadline):
        yield

    engine.admission.enter = admission_context
    engine.route_resources = resources
    monkeypatch.setattr(engine_module, "validate_request", lambda request, route: None)
    class ProxyPolicy:
        def __init__(self, *args): pass
        async def policy(self, credential_id): return ProxySelection("direct"), None
    monkeypatch.setattr(engine_module, "AccountProxyResolver", ProxyPolicy)
    monkeypatch.setattr(
        engine_module.routing_session, "select",
        lambda engine, principal, request, meta: asyncio.sleep(0, result=Selection(
            request, tuple(routes), scope, False, 2)),
    )
    monkeypatch.setattr(engine_module.routing_session, "revalidate", lambda *args: asyncio.sleep(0))

    with pytest.raises(GatewayError):
        await engine.prepare(object(), request, meta)
    assert adapter.calls == 2
    assert [event for event in events if event[0] == "enter"] == [
        ("enter", "provider"), ("enter", "provider"),
    ]


@asynccontextmanager
async def _empty_context():
    yield
