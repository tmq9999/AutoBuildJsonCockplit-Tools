import time

import httpx
import pytest


def success():
    return {"success": True, "code": 200, "timestamp": 1718029532890, "status": "SUCCESS", "data": {
        "realIpAddress": "93.184.216.34", "http": "93.184.216.34:39008", "socks5": "93.184.216.34:39009",
        "nextRequestAt": 1718029591927, "expirationAt": 1718030731927, "ttl": 1200, "ttc": 59,
        "httpPort": 39008, "socks5Port": 39009, "host": "93.184.216.34", "location": "Test"}}


def test_cooldown_never_interrupts_active_operation():
    from autobuild_json.gateway.proxy.kiot import lease_action
    assert lease_action(True, 1200, 0, 180, True) == "busy"
    assert lease_action(False, 500, 59, 180, False) == "reuse"
    assert lease_action(False, 30, 59, 180, False) == "wait"
    assert lease_action(False, 30, 0, 180, False) == "new"


@pytest.mark.asyncio
async def test_kiot_current_and_provider_errors_are_sanitized():
    from autobuild_json.gateway.proxy.kiot import KiotClient
    from autobuild_json.gateway.errors import GatewayError
    paths = []
    def respond(request):
        paths.append(request.url.path)
        if len(paths) == 1:
            return httpx.Response(200, json=success())
        return httpx.Response(200, json={"success": False, "code": 40400006,
            "message": "Key synthetic-sensitive-key not found", "error": "KEY_NOT_FOUND"})
    client = KiotClient(adapter=httpx.MockTransport(respond))
    lease = await client.current("synthetic-sensitive-key", protocol="http", deadline=time.monotonic()+10)
    assert lease.proxy.server == "http://93.184.216.34:39008"
    assert 1198 <= lease.ttl <= 1200
    assert lease.cooldown >= 59
    with pytest.raises(GatewayError) as exc:
        await client.current("synthetic-sensitive-key", protocol="http", deadline=time.monotonic()+10)
    assert exc.value.code == "kiot_key_invalid"
    assert "synthetic-sensitive-key" not in str(exc.value)
    assert paths == ["/api/v1/proxies/current"] * 2


@pytest.mark.asyncio
async def test_kiot_missing_current_returns_none_not_direct():
    from autobuild_json.gateway.proxy.kiot import KiotClient
    client = KiotClient(adapter=httpx.MockTransport(lambda r: httpx.Response(200, json={
        "success": False, "error": "PROXY_NOT_FOUND_BY_KEY", "code": 40001050})))
    assert await client.current("synthetic", protocol="http", deadline=time.monotonic()+10) is None


@pytest.mark.asyncio
async def test_kiot_missing_current_http_400_returns_none_not_unavailable():
    from autobuild_json.gateway.proxy.kiot import KiotClient
    client = KiotClient(adapter=httpx.MockTransport(lambda r: httpx.Response(400, json={
        "success": False, "error": "PROXY_NOT_FOUND_BY_KEY", "code": 40001050})))
    assert await client.current("synthetic", protocol="http", deadline=time.monotonic()+10) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error", [
    (302, "PROXY_NOT_FOUND_BY_KEY"),
    (401, "PROXY_NOT_FOUND_BY_KEY"),
    (403, "PROXY_NOT_FOUND_BY_KEY"),
    (500, "PROXY_NOT_FOUND_BY_KEY"),
    (503, "KEY_NOT_FOUND"),
])
async def test_kiot_http_failure_cannot_trigger_allocation_or_disable_key(status, error):
    from autobuild_json.gateway.proxy.kiot import KiotClient
    from autobuild_json.gateway.errors import GatewayError
    response = httpx.Response(status, json={"success": False, "error": error})
    client = KiotClient(adapter=httpx.MockTransport(lambda r: response))
    with pytest.raises(GatewayError) as result:
        await client.current("synthetic", protocol="http", deadline=time.monotonic()+10)
    assert result.value.code == "kiot_unavailable"
    assert response.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"success": True, "error": "PROXY_NOT_FOUND_BY_KEY"},
    {"success": "false", "error": "PROXY_NOT_FOUND_BY_KEY"},
    {"error": "PROXY_NOT_FOUND_BY_KEY"},
    {"success": False, "error": "SOME_OTHER_ERROR"},
    [],
])
async def test_kiot_http_400_requires_explicit_known_failure(payload):
    from autobuild_json.gateway.proxy.kiot import KiotClient
    from autobuild_json.gateway.errors import GatewayError
    client = KiotClient(adapter=httpx.MockTransport(lambda r: httpx.Response(400, json=payload)))
    with pytest.raises(GatewayError) as result:
        await client.current("synthetic", protocol="http", deadline=time.monotonic()+10)
    assert result.value.code == "kiot_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"", b"not-json", b"[]", b"x"*65537],
                         ids=["empty", "non-json", "array", "oversize"])
async def test_kiot_http_429_does_not_depend_on_response_body(body):
    from autobuild_json.gateway.proxy.kiot import KiotClient
    from autobuild_json.gateway.errors import GatewayError
    response = httpx.Response(429, content=body)
    client = KiotClient(adapter=httpx.MockTransport(lambda r: response))
    with pytest.raises(GatewayError) as result:
        await client.current("synthetic", protocol="http", deadline=time.monotonic()+10)
    assert result.value.code == "proxy_not_ready"
    assert response.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["new", "out"])
async def test_missing_proxy_is_not_success_on_other_operations(operation):
    from autobuild_json.gateway.proxy.kiot import KiotClient
    from autobuild_json.gateway.errors import GatewayError
    calls = []
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(400, json={"success": False, "error": "PROXY_NOT_FOUND_BY_KEY"})
    client = KiotClient(adapter=httpx.MockTransport(respond))
    with pytest.raises(GatewayError) as result:
        await client._call(operation, "synthetic", time.monotonic()+10)
    assert result.value.code == "kiot_unavailable"
    assert calls == ["/api/v1/proxies/"+operation]


@pytest.mark.asyncio
async def test_kiot_http_400_invalid_key_stays_sanitized():
    from autobuild_json.gateway.proxy.kiot import KiotClient
    from autobuild_json.gateway.errors import GatewayError
    response = httpx.Response(400, json={"success": False, "error": "KEY_NOT_FOUND",
        "message": "synthetic-sensitive-key"})
    client = KiotClient(adapter=httpx.MockTransport(lambda r: response))
    with pytest.raises(GatewayError) as result:
        await client.current("synthetic-sensitive-key", protocol="http", deadline=time.monotonic()+10)
    assert result.value.code == "kiot_key_invalid"
    assert "synthetic-sensitive-key" not in str(result.value)
    assert response.is_closed


@pytest.mark.parametrize("changes", [{"http": "127.0.0.1:80"}, {"http": "not-a-proxy"}, {"ttl": -1}, {"ttc": float("inf")}, {"expirationAt": 1}])
def test_kiot_rejects_invalid_endpoint_or_timing(changes):
    from autobuild_json.gateway.proxy.kiot import parse_lease
    payload = success()
    payload["data"].update(changes)
    with pytest.raises(ValueError):
        parse_lease(payload, "http", time.monotonic())


@pytest.mark.asyncio
async def test_kiot_does_not_log_query_key(caplog):
    import logging
    from autobuild_json.gateway.proxy.kiot import KiotClient
    caplog.set_level(logging.DEBUG)
    client = KiotClient(adapter=httpx.MockTransport(lambda r: httpx.Response(200, json=success())))
    await client.current("secret-never-log", protocol="http", deadline=time.monotonic()+10)
    assert "secret-never-log" not in caplog.text
