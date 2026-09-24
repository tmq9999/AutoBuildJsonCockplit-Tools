import asyncio
import logging
import time
import httpx
import pytest
from sqlalchemy import text

from autobuild_json.gateway.errors import GatewayError
from tests.gateway.catalog_support import discovery_case

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_later_429_discards_the_collected_prefix(pg_db):
    async def upstream(request):
        if request.url.params.get("after") == "first":
            return httpx.Response(429, headers={"Retry-After": "30"})
        return httpx.Response(200, json={"data": [{"id": "first"}],
                                         "has_more": True, "last_id": "first"})
    async with discovery_case(pg_db, upstream, mode="openai_cursor") as case:
        with pytest.raises(GatewayError, match="rate_limited") as error:
            await case.client.collect(case.context, case.lease, case.check_current, case.deadline)
        assert error.value.retry_after == 30
        assert case.request_paths == ["/v1/models", "/v1/models"]
        async with pg_db.sessions() as session:
            rows = (await session.execute(text("SELECT active FROM provider_admissions ORDER BY admitted_at"))).scalars().all()
        assert rows == [False, False]


async def test_check_reads_only_first_page_and_marks_auth_unverified(pg_db):
    async def upstream(request):
        return httpx.Response(200, json={"data": [{"id": "first"}], "has_more": True, "last_id": "first"})
    async with discovery_case(pg_db, upstream, mode="openai_cursor") as case:
        result = await case.client.check(case.context, case.lease, case.check_current, case.deadline)
        assert result["reachable"] is True
        assert result["catalog_readable"] is True
        assert result["authentication"] == "unverified"
        assert result["status_code"] == 200
        assert case.request_paths == ["/v1/models"]


@pytest.mark.parametrize("mode,first,next_page", [
    ("openai_cursor", {"data": [{"id": "a"}], "has_more": True, "last_id": "a"},
     {"data": [{"id": "b"}], "has_more": False}),
    ("anthropic", {"data": [{"id": "a"}], "has_more": True, "last_id": "a"},
     {"data": [{"id": "b"}], "has_more": False}),
    ("gemini", {"models": [{"name": "models/a"}], "nextPageToken": "next"},
     {"models": [{"name": "models/b"}]}),
])
async def test_collects_complete_pages_for_each_cursor_mode(pg_db, mode, first, next_page):
    seen = []
    async def observed(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=next_page if len(seen) == 2 else first)
    async with discovery_case(pg_db, observed, mode=mode) as case:
        entries = await case.client.collect(case.context, case.lease, case.check_current, case.deadline)
        assert [entry.upstream_id for entry in entries] == ["a", "b"]
        assert len(seen) == 2
        assert len(case.credential_calls) == 1


async def test_repeated_cursor_discards_prefix(pg_db):
    async def upstream(request):
        return httpx.Response(200, json={"data": [{"id": "a"}], "has_more": True, "last_id": "a"})
    async with discovery_case(pg_db, upstream, mode="openai_cursor") as case:
        with pytest.raises(GatewayError, match="catalog_incomplete"):
            await case.client.collect(case.context, case.lease, case.check_current, case.deadline)
        assert len(case.request_paths) == 2


async def test_page_cap_discards_prefix(pg_db):
    async def upstream(request):
        number = len(seen)
        seen.append(number)
        return httpx.Response(200, json={"data": [{"id": f"id-{number}"}],
                                         "has_more": True, "last_id": f"id-{number}"})
    seen = []
    async with discovery_case(pg_db, upstream, mode="openai_cursor") as case:
        with pytest.raises(GatewayError, match="catalog_limit_exceeded"):
            await case.client.collect(case.context, case.lease, case.check_current, case.deadline)
        assert len(seen) == 10


async def test_unique_id_cap_discards_prefix(pg_db):
    async def upstream(request):
        if request.url.params.get("after"):
            return httpx.Response(200, json={"data": [{"id": "id-10000"}], "has_more": False})
        return httpx.Response(200, json={"data": [{"id": f"id-{i}"} for i in range(10000)],
                                         "has_more": True, "last_id": "id-9999"})
    async with discovery_case(pg_db, upstream, mode="openai_cursor") as case:
        with pytest.raises(GatewayError, match="catalog_limit_exceeded"):
            await case.client.collect(case.context, case.lease, case.check_current, case.deadline)
        assert len(case.request_paths) == 2


async def test_total_decoded_body_cap_discards_prefix(pg_db):
    blob = "x" * 1_900_000
    sent = 0
    async def upstream(request):
        nonlocal sent
        sent += 1
        return httpx.Response(200, json={"data": [{"id": f"id-{sent}"}],
                                         "has_more": True, "last_id": f"id-{sent}", "ignored": blob})
    async with discovery_case(pg_db, upstream, mode="openai_cursor") as case:
        with pytest.raises(GatewayError, match="catalog_limit_exceeded"):
            await case.client.collect(case.context, case.lease, case.check_current, case.deadline)
        assert sent == 5


async def test_credential_is_resolved_once_across_pages(pg_db):
    async def upstream(request):
        assert request.headers["authorization"] == "Bearer synthetic-upstream-secret"
        if request.url.params.get("after"):
            return httpx.Response(200, json={"data": [{"id": "b"}], "has_more": False})
        return httpx.Response(200, json={"data": [{"id": "a"}], "has_more": True, "last_id": "a"})
    async with discovery_case(pg_db, upstream, mode="openai_cursor") as case:
        await case.client.collect(case.context, case.lease, case.check_current, case.deadline)
        assert len(case.credential_calls) == 1


async def test_deadline_includes_current_context_refresh(pg_db):
    async def upstream(request):
        pytest.fail("HTTP must not start after deadline")
    async with discovery_case(pg_db, upstream) as case:
        async def slow_current():
            await asyncio.sleep(0.2)
        started = time.monotonic()
        with pytest.raises(GatewayError, match="deadline_exceeded"):
            await case.client.collect(case.context, case.lease, slow_current, time.monotonic()+0.01)
        assert time.monotonic()-started < 0.1
        assert case.request_paths == []


@pytest.mark.parametrize("proxy_mode", ["fixed", "pool"])
async def test_proxy_lease_remains_sticky_across_pages(pg_db, proxy_mode):
    from autobuild_json.models import ProxyConfig
    from autobuild_json.gateway.proxy.config import ProxySelection
    from autobuild_json.gateway.proxy.manager import endpoint_resource
    entries = (ProxyConfig("http://93.184.216.34:39008", username="proxy-user", password="proxy-pass"),)
    if proxy_mode == "pool":
        entries += (ProxyConfig("http://93.184.216.35:39008"),)
    selection = ProxySelection(proxy_mode, runtime_entries=entries)
    async def upstream(request):
        if request.url.params.get("after"):
            return httpx.Response(200, json={"data": [{"id": "b"}], "has_more": False})
        return httpx.Response(200, json={"data": [{"id": "a"}], "has_more": True, "last_id": "a"})
    async with discovery_case(pg_db, upstream, mode="openai_cursor", selection=selection) as case:
        chosen = case.lease.proxy
        assert chosen in entries
        assert len(await case.client.collect(case.context, case.lease,
                                             case.check_current, case.deadline)) == 2
        assert case.lease.proxy is chosen
        async with pg_db.sessions() as session:
            held = await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE resource=:resource AND owner IS NOT NULL"),
                                        {"resource": endpoint_resource(chosen)})
        assert held == 1
    async with pg_db.sessions() as session:
        released = await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL"))
    assert released == 0


async def test_kiot_current_once_and_no_rotation_after_429(pg_db):
    from autobuild_json.gateway.proxy.config import ProxySelection, parse_kiot_keys
    from autobuild_json.gateway.proxy.kiot import KiotClient
    from tests.gateway.unit.test_kiot import success
    calls = []
    def kiot_upstream(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=success())
    kiot = KiotClient(adapter=httpx.MockTransport(kiot_upstream))
    selection = ProxySelection("kiotproxy", runtime_entries=tuple(
        parse_kiot_keys("synthetic-kiot-key", pepper=b"p"*32)))
    async def provider_upstream(request):
        if request.url.params.get("after"):
            return httpx.Response(429)
        return httpx.Response(200, json={"data": [{"id": "a"}], "has_more": True, "last_id": "a"})
    async with discovery_case(pg_db, provider_upstream, mode="openai_cursor", selection=selection, kiot=kiot) as case:
        assert case.lease.proxy.server == "http://93.184.216.34:39008"
        with pytest.raises(GatewayError, match="rate_limited"):
            await case.client.collect(case.context, case.lease, case.check_current, case.deadline)
    assert calls == ["/api/v1/proxies/current"]


async def test_redirect_does_not_send_to_second_origin(pg_db):
    async def upstream(request):
        return httpx.Response(302, headers={"Location": "https://evil.invalid/steal"})
    async with discovery_case(pg_db, upstream) as case:
        with pytest.raises(GatewayError, match="upstream_error"):
            await case.client.collect(case.context, case.lease, case.check_current, case.deadline)
        assert case.request_paths == ["/v1/models"]


async def test_none_auth_never_resolves_credential(pg_db):
    async def upstream(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"models": [{"name": "local"}]})
    async with discovery_case(pg_db, upstream, mode="ollama") as case:
        async def forbidden(_route):
            raise AssertionError("credential resolver called")
        case.client.credential_resolver = forbidden
        assert [entry.upstream_id for entry in await case.client.collect(
            case.context, case.lease, case.check_current, case.deadline)] == ["local"]


async def test_hostile_cursor_and_secret_do_not_enter_debug_logs(pg_db, caplog):
    async def upstream(request):
        if not request.url.params.get("after"):
            return httpx.Response(200, json={"data": [{"id": "a"}], "has_more": True,
                                             "last_id": "opaque-SECRET-marker"})
        return httpx.Response(200, json={"data": [{"id": "b"}], "has_more": False})
    with caplog.at_level(logging.DEBUG):
        async with discovery_case(pg_db, upstream, mode="openai_cursor") as case:
            await case.client.collect(case.context, case.lease, case.check_current, case.deadline)
    assert "opaque-SECRET-marker" not in caplog.text
    assert "synthetic-upstream-secret" not in caplog.text


async def test_concurrent_calls_keep_warnings_per_operation(pg_db):
    count = 0
    async def upstream(request):
        nonlocal count
        count += 1
        await asyncio.sleep(0)
        return httpx.Response(200, json={"models": [{"name": "a", "inputTokenLimit":
                                        "bad" if count == 1 else 4096}]})
    async with discovery_case(pg_db, upstream, mode="gemini") as case:
        warnings_a, warnings_b = [], []
        await asyncio.gather(
            case.client.collect(case.context, case.lease, case.check_current, case.deadline,
                                on_warnings=warnings_a.extend),
            case.client.collect(case.context, case.lease, case.check_current, case.deadline,
                                on_warnings=warnings_b.extend),
        )
        assert sorted((warnings_a, warnings_b)) == [[], ["malformed_optional_metadata"]]


@pytest.mark.parametrize("mode,payload,key", [
    ("gemini", {"models": [{"name": "models/native", "inputTokenLimit": 4096,
                            "outputTokenLimit": 1024}]}, "input_token_limit"),
    ("openai_single", {"data": [{"id": "relay", "pricing": {
        "currency": "USD", "unit": "per_token", "input": "0.01", "output": "0.02"}}]}, "pricing"),
])
async def test_normalized_metadata_survives_collector_to_snapshot(pg_db, mode, payload, key):
    from uuid import uuid4
    from autobuild_json.gateway.catalog.operations import OperationStore
    from autobuild_json.gateway.catalog.records import OperationRequest
    from autobuild_json.gateway.catalog.snapshots import SnapshotStore
    from tests.gateway.catalog_support import synthetic_vault

    async def upstream(request):
        return httpx.Response(200, json=payload)
    async with discovery_case(pg_db, upstream, mode=mode) as case:
        entries = await case.client.collect(case.context, case.lease, case.check_current, case.deadline)
        ops = OperationStore(pg_db, case.sources)
        store = SnapshotStore(pg_db, case.sources, ops,
                              synthetic_vault().derive_key("client_keys", "catalog-cursor-v1"))
        request = OperationRequest(id=uuid4(), source_id=case.source.id,
                                   kind="discover", expected=case.context.stamp)
        operation = await ops.enqueue(request, "test-admin")
        claim = await ops.claim(operation.id, uuid4())
        await store.commit(claim, case.context, entries)
        page = await store.page(case.source.id)
        assert key in page.items[0].entry.metadata


async def test_safe_result_accepts_bounded_metadata_warning():
    from autobuild_json.gateway.catalog.operations import _safe_result
    assert _safe_result({"warnings": ["malformed_optional_metadata"]}) == (
        '{"warnings":["malformed_optional_metadata"]}')
    with pytest.raises(GatewayError, match="invalid_request"):
        _safe_result({"warnings": [None]})
