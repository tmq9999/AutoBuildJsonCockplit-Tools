import time
from types import SimpleNamespace

import httpx
import pytest


async def policy():
    from autobuild_json.gateway.transport.egress import EgressPolicy
    async def resolver(host, port):
        return ["93.184.216.34"]
    return EgressPolicy(resolver=resolver)


@pytest.mark.asyncio
async def test_transport_never_forwards_client_auth_or_follows_redirect():
    from autobuild_json.gateway.transport.http import Transport, OutboundRequest
    received = []
    def respond(request):
        received.append(request)
        return httpx.Response(302, headers={"Location": "https://other.invalid"})
    transport = Transport(await policy(), adapter=httpx.MockTransport(respond))
    call = OutboundRequest("POST", "chat/completions", {"model": "m"},
                           auth_header=("Authorization", "Bearer synthetic-upstream"),
                           deadline=time.monotonic() + 10)
    async with transport.open(SimpleNamespace(root="https://provider.invalid/v1"), None, call) as response:
        assert response.status == 302
    assert len(received) == 1
    assert received[0].headers["Authorization"] == "Bearer synthetic-upstream"
    assert "cookie" not in received[0].headers
    assert str(received[0].url) == "https://provider.invalid/v1/chat/completions"


@pytest.mark.asyncio
async def test_transport_bounds_body_and_sanitizes_network_failure():
    from autobuild_json.gateway.transport.http import Transport, OutboundRequest
    from autobuild_json.gateway.errors import GatewayError
    transport = Transport(await policy(), adapter=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x"*100)))
    call = OutboundRequest("GET", "models", None, deadline=time.monotonic() + 10)
    async with transport.open(SimpleNamespace(root="https://provider.invalid/v1"), None, call) as response:
        with pytest.raises(GatewayError, match="upstream_error"):
            await response.read_json(max_bytes=10)
    def failed(request):
        raise httpx.ConnectError("secret=should-not-leak", request=request)
    transport = Transport(await policy(), adapter=httpx.MockTransport(failed))
    with pytest.raises(GatewayError) as exc:
        async with transport.open(SimpleNamespace(root="https://provider.invalid/v1"), None, call):
            pass
    assert "should-not-leak" not in str(exc.value)


@pytest.mark.asyncio
async def test_proxy_connect_failure_before_response_is_safe_retryable():
    from autobuild_json.gateway.transport.egress import EgressPolicy
    from autobuild_json.gateway.transport.http import Transport, OutboundRequest
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.models import ProxyConfig

    async def resolver(host, port):
        return ["93.184.216.34"]

    def failed(request):
        raise httpx.ConnectError("opaque-connect-detail", request=request)

    transport = Transport(EgressPolicy(resolver=resolver, trusted_proxy_origins={"http://proxy.invalid:8080"}),
                          adapter=httpx.MockTransport(failed))
    with pytest.raises(GatewayError) as caught:
        async with transport.open(SimpleNamespace(root="https://provider.invalid/v1"),
                                  ProxyConfig("http://proxy.invalid:8080"),
                                  OutboundRequest("POST", "chat/completions", {}, deadline=time.monotonic() + 10)):
            pass

    error = caught.value
    assert error.code == "proxy_error"
    assert error.safe_retry is True
    assert error.transport_phase == "before_response"
    assert error.proxy_used is True
    assert "opaque-connect-detail" not in str(error)


@pytest.mark.asyncio
async def test_proxy_stream_reset_after_first_chunk_is_not_retryable():
    from autobuild_json.gateway.transport.egress import EgressPolicy
    from autobuild_json.gateway.transport.http import Transport, OutboundRequest
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.models import ProxyConfig

    class Reset(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            raise httpx.ReadError("opaque-reset-detail")

        async def aclose(self):
            return None

    async def resolver(host, port):
        return ["93.184.216.34"]

    transport = Transport(
        EgressPolicy(resolver=resolver, trusted_proxy_origins={"http://proxy.invalid:8080"}),
        adapter=httpx.MockTransport(lambda request: httpx.Response(200, stream=Reset())),
    )
    async with transport.open(SimpleNamespace(root="https://provider.invalid/v1"),
                              ProxyConfig("http://proxy.invalid:8080"),
                              OutboundRequest("GET", "models", None, deadline=time.monotonic() + 10)) as response:
        chunks = response.chunks()
        assert await chunks.__anext__() == b"first"
        with pytest.raises(GatewayError) as caught:
            await chunks.__anext__()

    error = caught.value
    assert error.code == "proxy_error"
    assert error.safe_retry is False
    assert error.transport_phase == "after_response"
    assert error.proxy_used is True
    assert "opaque-reset-detail" not in str(error)


@pytest.mark.asyncio
async def test_proxy_requires_explicit_trusted_remote_dns_policy():
    from autobuild_json.models import ProxyConfig
    from autobuild_json.gateway.transport.http import Transport, OutboundRequest
    from autobuild_json.gateway.errors import GatewayError
    transport = Transport(await policy(), adapter=httpx.MockTransport(lambda request: httpx.Response(200)))
    with pytest.raises(GatewayError, match="egress_denied"):
        async with transport.open(SimpleNamespace(root="https://provider.invalid/v1"),
                                  ProxyConfig("http://proxy.invalid:8080"),
                                  OutboundRequest("GET", "models", None, deadline=time.monotonic()+10)):
            pass


@pytest.mark.parametrize("suffix", ["../admin", "https://other.invalid", "/models", "models?key=x", "models#x"])
def test_outbound_suffix_cannot_replace_api_root(suffix):
    from autobuild_json.gateway.transport.http import OutboundRequest
    with pytest.raises(ValueError):
        OutboundRequest("GET", suffix, None, deadline=time.monotonic()+10)


@pytest.mark.asyncio
async def test_total_deadline_includes_dns_resolution():
    import asyncio
    from autobuild_json.gateway.transport.egress import EgressPolicy
    from autobuild_json.gateway.transport.http import Transport, OutboundRequest
    from autobuild_json.gateway.errors import GatewayError
    cancelled = asyncio.Event()
    async def resolver(host, port):
        try:
            await asyncio.sleep(0.1)
            return ["93.184.216.34"]
        finally:
            cancelled.set()
    transport = Transport(EgressPolicy(resolver=resolver), adapter=httpx.MockTransport(lambda r: httpx.Response(200)))
    started = time.monotonic()
    with pytest.raises(GatewayError, match="deadline_exceeded"):
        async with transport.open(SimpleNamespace(root="https://provider.invalid"), None,
                                  OutboundRequest("GET", "models", None, deadline=started+0.01)):
            pass
    assert time.monotonic() - started < 0.08
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_stream_timeout_closes_upstream():
    import asyncio
    from autobuild_json.gateway.transport.http import Transport, OutboundRequest
    from autobuild_json.gateway.errors import GatewayError
    class Slow(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield b"first"
            await asyncio.sleep(1)
            yield b"second"
        async def aclose(self):
            self.closed = True
    stream = Slow()
    transport = Transport(await policy(), adapter=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream)))
    with pytest.raises(GatewayError, match="deadline_exceeded"):
        async with transport.open(SimpleNamespace(root="https://provider.invalid"), None,
                                  OutboundRequest("GET", "models", None, deadline=time.monotonic()+0.02)) as response:
            iterator = response.chunks()
            assert await iterator.__anext__() == b"first"
            await iterator.__anext__()
    assert stream.closed
