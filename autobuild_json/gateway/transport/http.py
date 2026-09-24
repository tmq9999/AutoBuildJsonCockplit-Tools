import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import ssl
import time
from urllib.parse import unquote, urlsplit
from urllib.parse import urlencode

import httpcore
import httpx

from ..errors import GatewayError
from .network import GuardedBackend


@dataclass(frozen=True)
class OutboundRequest:
    method: str
    suffix: str
    body: dict | None = field(repr=False)
    auth_header: tuple[str, str] | None = field(default=None, repr=False)
    deadline: float = 0
    headers: tuple[tuple[str, str], ...] = ()
    query: tuple[tuple[str, str], ...] = ()
    kind: str = "inference"
    discovery_mode: str | None = None

    def __post_init__(self):
        if (self.method not in {"GET", "POST"} or not self.suffix or self.suffix.startswith("/")
                or any(c in self.suffix for c in "?#\\") or "://" in self.suffix
                or any(part in {".", ".."} for part in unquote(self.suffix).split("/"))
                or any(ord(c) <= 32 for c in self.suffix)):
            raise ValueError("invalid_upstream_path")
        allowed = {"accept", "content-type", "anthropic-version", "chatgpt-account-id", "originator"}
        if self.kind == "discovery":
            from ..providers.discovery.pagination import SUFFIXES, valid_mode, validate_query
            mode = valid_mode(self.discovery_mode)
            if self.method != "GET" or self.body is not None or self.suffix != SUFFIXES[mode]:
                raise ValueError("invalid_discovery_request")
            validate_query(mode, self.query)
        elif self.kind == "inference" and self.discovery_mode is None:
            if self.query not in {(), (("alt", "sse"),)}:
                raise ValueError("invalid_upstream_query")
        else:
            raise ValueError("invalid_request_kind")
        if any(name.lower() not in allowed or "\r" in value or "\n" in value for name, value in self.headers):
            raise ValueError("invalid_upstream_headers")
        if self.auth_header is not None:
            name, value = self.auth_header
            if name.lower() not in {"authorization", "x-api-key", "x-goog-api-key"} or any(ord(c) < 32 for c in value):
                raise ValueError("invalid_upstream_auth")


class CoreStream(httpx.AsyncByteStream):
    def __init__(self, response):
        self.response = response

    async def __aiter__(self):
        async for chunk in self.response.aiter_stream():
            yield chunk

    async def aclose(self):
        await self.response.aclose()


class CoreTransport(httpx.AsyncBaseTransport):
    def __init__(self, policy, proxy):
        options = {"ssl_context": ssl.create_default_context(), "network_backend": GuardedBackend(policy),
                   "max_connections": 16, "max_keepalive_connections": 0, "retries": 0}
        if proxy is None:
            self.pool = httpcore.AsyncConnectionPool(**options)
        else:
            auth = (proxy.username.encode(), (proxy.password or "").encode()) if proxy.username else None
            p = urlsplit(proxy.server)
            if p.scheme in {"socks5", "socks5h"}:
                self.pool = httpcore.AsyncSOCKSProxy(proxy_url=proxy.server.replace("socks5h://", "socks5://"),
                                                   proxy_auth=auth, **options)
            else:
                self.pool = httpcore.AsyncHTTPProxy(proxy_url=proxy.server, proxy_auth=auth,
                    proxy_ssl_context=ssl.create_default_context() if p.scheme == "https" else None, **options)

    async def handle_async_request(self, request):
        core_request = httpcore.Request(method=request.method, url=httpcore.URL(
            scheme=request.url.raw_scheme, host=request.url.raw_host, port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions)
        response = await self.pool.handle_async_request(core_request)
        return httpx.Response(response.status, headers=response.headers, stream=CoreStream(response), extensions=response.extensions)

    async def aclose(self):
        await self.pool.aclose()


class UpstreamResponse:
    def __init__(self, raw, deadline):
        self.raw, self.deadline = raw, deadline
        self.status = raw.status_code
        self.headers = raw.headers

    async def chunks(self):
        iterator = self.raw.aiter_bytes().__aiter__()
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise GatewayError("deadline_exceeded", 504, "upstream")
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=min(60, remaining))
            except StopAsyncIteration:
                return
            except (asyncio.TimeoutError, httpx.TimeoutException, httpcore.TimeoutException):
                raise GatewayError("deadline_exceeded", 504, "upstream") from None
            yield chunk

    async def read_json(self, max_bytes=2_097_152):
        data = bytearray()
        async for chunk in self.chunks():
            if len(data) + len(chunk) > max_bytes:
                raise GatewayError("upstream_error", 502, "upstream")
            data.extend(chunk)
        try:
            return json.loads(data)
        except (ValueError, UnicodeError):
            raise GatewayError("upstream_error", 502, "upstream") from None

    async def close(self):
        await self.raw.aclose()

    async def cancel(self):
        await self.close()


class Transport:
    def __init__(self, policy, *, adapter=None):
        self.policy, self.adapter = policy, adapter

    async def _validate(self, route, proxy):
        try:
            await self.policy.resolve_and_check(route.root)
            if proxy is not None:
                p = urlsplit(proxy.server)
                origin = f"{p.scheme}://{p.netloc}"
                if origin not in self.policy.trusted_proxy_origins and getattr(route, "proxy_profile_id", None) is None:
                    raise ValueError()
                await self.policy.addresses(p.hostname, p.port)
        except ValueError:
            raise GatewayError("egress_denied", 502, "upstream") from None

    @asynccontextmanager
    async def open(self, route, proxy, request):
        try:
            remaining = request.deadline - time.monotonic()
            if remaining <= 0:
                raise GatewayError("deadline_exceeded", 504, "upstream")
            await asyncio.wait_for(self._validate(route, proxy), remaining)
            remaining = request.deadline - time.monotonic()
            headers = {"accept": "application/json", "content-type": "application/json"}
            headers.update(request.headers)
            if request.auth_header:
                headers[request.auth_header[0]] = request.auth_header[1]
            adapter = self.adapter or CoreTransport(self.policy, proxy)
            if request.kind == "discovery":
                # AsyncClient logs full URLs at INFO, including opaque cursors.
                # Handle the guarded transport directly for this request kind.
                try:
                    url = route.root.rstrip("/") + "/" + request.suffix + ("?"+urlencode(request.query) if request.query else "")
                    timeout = {"connect": min(10, remaining), "read": min(60, remaining),
                               "write": min(60, remaining), "pool": min(60, remaining)}
                    call = httpx.Request(request.method, url, headers=headers, extensions={"timeout": timeout})
                    response = await asyncio.wait_for(adapter.handle_async_request(call), timeout=remaining)
                    try:
                        yield UpstreamResponse(response, request.deadline)
                    finally:
                        await response.aclose()
                finally:
                    if self.adapter is None:
                        await adapter.aclose()
                return
            async with httpx.AsyncClient(transport=adapter, trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(min(60, remaining), connect=min(10, remaining))) as client:
                url = route.root.rstrip("/") + "/" + request.suffix + ("?"+urlencode(request.query) if request.query else "")
                call = client.build_request(request.method, url,
                                            json=request.body, headers=headers)
                response = await asyncio.wait_for(client.send(call, stream=True), timeout=remaining)
                try:
                    yield UpstreamResponse(response, request.deadline)
                finally:
                    await response.aclose()
        except GatewayError:
            raise
        except ValueError:
            raise GatewayError("egress_denied", 502, "upstream") from None
        except (asyncio.TimeoutError, httpx.TimeoutException, httpcore.TimeoutException):
            raise GatewayError("deadline_exceeded", 504, "upstream") from None
        except (httpx.HTTPError, httpcore.NetworkError, httpcore.ProtocolError, OSError):
            raise GatewayError("proxy_error" if proxy else "upstream_error", 502, "upstream") from None
