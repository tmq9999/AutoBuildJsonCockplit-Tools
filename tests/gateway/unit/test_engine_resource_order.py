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
    assert caught.value.stage == "proxy"
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
async def test_route_resources_maps_provider_deadline_to_provider_stage():
    from datetime import datetime, timedelta, timezone
    from autobuild_json.gateway.engine import Engine
    from autobuild_json.gateway.errors import GatewayError

    class ProviderAdmission:
        @asynccontextmanager
        async def acquire(self, route, deadline, *, wait=False):
            raise GatewayError("deadline_exceeded", 504, "upstream")
            yield  # pragma: no cover

    engine = Engine.__new__(Engine)
    from autobuild_json.gateway.admission import InferenceAdmission

    engine.admission = InferenceAdmission(1)
    engine._record_stage_outcome = Engine._record_stage_outcome.__get__(engine)
    engine._record_upstream_outcome = Engine._record_upstream_outcome.__get__(engine)
    engine.provider_limits = ProviderAdmission()
    engine.proxies = SimpleNamespace()
    with pytest.raises(GatewayError) as caught:
        async with engine.route_resources(
            "route", SimpleNamespace(mode="direct"), "owner",
            datetime.now(timezone.utc) + timedelta(seconds=1),
        ):
            pass
    assert caught.value.code == "deadline_exceeded"
    assert caught.value.stage == "provider"
    assert engine.admission.snapshot()["deadline_expired_by_stage"] == {
        "provider": 1, "proxy": 0, "upstream": 0,
    }


@pytest.mark.asyncio
async def test_route_resources_uses_one_deadline_across_provider_and_proxy_waits():
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
            events.append("proxy.claim")
            raise ProxyCapacityError()
            yield  # pragma: no cover

        async def wait_for_capacity(self, selection, owner, deadline):
            await asyncio.sleep(.05)

    engine = Engine.__new__(Engine)
    from autobuild_json.gateway.admission import InferenceAdmission

    engine.admission = InferenceAdmission(1)
    engine.provider_limits = ProviderAdmission()
    engine.proxies = ProxyLeases()
    started = asyncio.get_running_loop().time()
    deadline = datetime.now(timezone.utc) + timedelta(milliseconds=25)
    with pytest.raises(GatewayError) as caught:
        async with engine.route_resources(
            "route", SimpleNamespace(mode="fixed"), "owner", deadline,
        ):
            pass
    assert caught.value.code == "deadline_exceeded"
    assert caught.value.stage == "proxy"
    assert asyncio.get_running_loop().time() - started < .08
    assert events == ["provider.enter", "proxy.claim", "provider.exit"]
    assert engine.admission.snapshot()["deadline_expired_by_stage"] == {
        "provider": 0, "proxy": 1, "upstream": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_outcome"),
    [
        ("deadline", "expired"),
        ("rate_limited", "rate_limited"),
        ("cancelled", "cancelled"),
    ],
)
async def test_route_resources_attributes_post_acquisition_outcome_once_to_upstream(
    failure, expected_outcome,
):
    from datetime import datetime, timedelta, timezone
    from autobuild_json.gateway.admission import InferenceAdmission
    from autobuild_json.gateway.engine import Engine
    from autobuild_json.gateway.errors import GatewayError

    class ProviderAdmission:
        @asynccontextmanager
        async def acquire(self, route, deadline, *, wait=False):
            yield object()

    class ProxyLeases:
        @asynccontextmanager
        async def acquire(self, selection, owner, deadline, *, wait=False):
            yield SimpleNamespace(proxy=None)

    engine = Engine.__new__(Engine)
    engine.admission = InferenceAdmission(2)
    engine.provider_limits = ProviderAdmission()
    engine.proxies = ProxyLeases()
    engine.transport = SimpleNamespace(_validate=lambda route, proxy: asyncio.sleep(0))
    error = (asyncio.CancelledError() if failure == "cancelled" else
             GatewayError("deadline_exceeded", 504, "request") if failure == "deadline" else
             GatewayError("rate_limited", 429, "upstream", 1))
    caught = None
    try:
        async with engine.route_resources(
            "route", SimpleNamespace(mode="direct"), "owner",
            datetime.now(timezone.utc) + timedelta(seconds=1),
        ):
            raise error
    except BaseException as exc:
        caught = exc

    assert caught is error
    if failure == "deadline":
        assert caught.stage == "upstream"
    snapshot = engine.admission.snapshot()
    field = {
        "expired": "deadline_expired_by_stage",
        "cancelled": "cancelled_by_stage",
        "rate_limited": "rate_limited_by_stage",
    }[expected_outcome]
    assert snapshot[field] == {"provider": 0, "proxy": 0, "upstream": 1}
    assert snapshot["resource_wait_expired"] == 0
    assert snapshot["resource_wait_cancelled"] == 0


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
    from autobuild_json.gateway.admission import InferenceAdmission

    engine.admission = InferenceAdmission(1)
    engine._record_stage_outcome = Engine._record_stage_outcome.__get__(engine)
    engine._record_upstream_outcome = Engine._record_upstream_outcome.__get__(engine)
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
    assert engine.admission.snapshot()["failed_by_stage"] == {
        "provider": 0, "proxy": 0, "upstream": 1,
    }


@pytest.mark.asyncio
async def test_prepare_revalidate_failure_is_not_reported_as_upstream(monkeypatch):
    import autobuild_json.gateway.engine as engine_module
    from autobuild_json.gateway.engine import Engine, RequestMeta
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.routing.session import Selection
    from autobuild_json.gateway.admission import InferenceAdmission

    route = SimpleNamespace(
        adapter="fake", provider_id="provider", credential_id="credential",
        proxy_profile_id=None, public_model_id="public", binding_id="binding",
        upstream_model="upstream", budget_id=None,
        bounds=SimpleNamespace(input_tokens=1, output_tokens=2), input_micro=1, output_micro=1,
        cache_read_micro=None, cache_write_micro=None, timeout=60,
    )
    options = SimpleNamespace(max_output_tokens=None, model_copy=lambda **kwargs: options)
    request = SimpleNamespace(model="public", options=options)
    request.model_copy = lambda **kwargs: request
    meta = RequestMeta(uuid4(), datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(seconds=30))
    opened = []

    class Ledger:
        async def reserve(self, admission): return "hold"
        async def mark_dispatched(self, *args, **kwargs): pass
        async def mark_rejected(self, *args): pass
        async def release_unspent(self, *args): pass
        async def reject_and_release(self, *args): pass

    class Adapter:
        @asynccontextmanager
        async def open(self, *args):
            opened.append(True)
            yield object()

    engine = Engine.__new__(Engine)
    engine.catalog = SimpleNamespace(visible_model=lambda *args: asyncio.sleep(0, result="public"))
    engine.ledger = Ledger()
    engine.admission = InferenceAdmission(1)
    engine.budgets = SimpleNamespace()
    engine.adapters = {"fake": Adapter()}
    engine.credentials = SimpleNamespace(_record=lambda *args: asyncio.sleep(0), fresh_tokens=lambda *args: asyncio.sleep(0))
    engine.credential = lambda *args: asyncio.sleep(0)
    engine.proxy_resolver = None
    engine.db = None
    engine._record_stage_outcome = Engine._record_stage_outcome.__get__(engine)
    engine._record_upstream_outcome = Engine._record_upstream_outcome.__get__(engine)

    @asynccontextmanager
    async def resources(*args):
        yield SimpleNamespace(proxy=None)

    engine.route_resources = resources
    monkeypatch.setattr(engine_module, "validate_request", lambda *args: None)
    monkeypatch.setattr(engine_module.routing_session, "select", lambda *args: asyncio.sleep(0, result=Selection(
        request, (route,), SimpleNamespace(public_model_id="public"), False, 0)))
    monkeypatch.setattr(engine_module.routing_session, "revalidate",
                        lambda *args: asyncio.sleep(0, result=None) if len(args) < 5 else
                        asyncio.sleep(0, result=None))
    # The second fence is a pre-I/O policy/config failure.
    calls = 0
    async def revalidate(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GatewayError("invalid_state", 409, "policy")
    monkeypatch.setattr(engine_module.routing_session, "revalidate", revalidate)

    with pytest.raises(GatewayError) as caught:
        await engine.prepare(object(), request, meta)
    assert caught.value.code == "invalid_state"
    assert caught.value.stage == "policy"
    assert opened == []
    assert engine.admission.snapshot()["failed_by_stage"] == {
        "provider": 0, "proxy": 0, "upstream": 0,
    }


@asynccontextmanager
async def _empty_context():
    yield
