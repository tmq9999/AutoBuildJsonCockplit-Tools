"""Responses WebSocket over the same guarded TLS/proxy transport as HTTP.

httpcore owns DNS pinning, CONNECT/SOCKS authentication and TLS verification;
wsproto owns the handshake and frame validation. No unguarded second connector,
environment proxy discovery, redirects or direct fallback.
"""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
import json
import time

import httpcore
import httpx
from wsproto import ConnectionType, WSConnection
from wsproto.events import Request, AcceptConnection, TextMessage, Ping, Pong
from wsproto.utilities import RemoteProtocolError

from ..errors import GatewayError, TransportFailure, UpstreamRejected
from ..providers.retry import parse_retry_after
from .http import CoreTransport, _account_close

MAX_MESSAGE = 40_000_000


class CodexSocket:
    def __init__(self, protocol, stream, deadline, *, proxy_used=False):
        self.protocol, self.stream, self.deadline, self.proxy_used = protocol, stream, deadline, bool(proxy_used)
        self.pending = deque()
        self.closed = False
        self._close_task = None

    async def _io(self, operation):
        remaining = self.deadline - time.monotonic()
        try:
            return await asyncio.wait_for(operation, max(0, min(60, remaining)))
        except (asyncio.TimeoutError, httpcore.TimeoutException):
            raise GatewayError("deadline_exceeded", 504, "upstream") from None
        except (httpcore.NetworkError, httpcore.ProtocolError, OSError):
            # A WebSocket stream has already completed its handshake by the
            # time CodexSocket performs I/O, so all resets are ambiguous.
            raise TransportFailure(before_response=False, proxy_used=self.proxy_used) from None

    async def send_json(self, value):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        await self._io(self.stream.write(self.protocol.send(TextMessage(data=payload))))

    async def receive_json(self):
        parts, size = [], 0
        while True:
            if not self.pending:
                data = await self._io(self.stream.read(65536))
                if not data:
                    raise GatewayError("upstream_error", 502, "stream")
                self.protocol.receive_data(data)
                self.pending.extend(self.protocol.events())
            while self.pending:
                event = self.pending.popleft()
                if isinstance(event, Ping):
                    await self._io(self.stream.write(self.protocol.send(event.response())))
                elif isinstance(event, Pong):
                    continue
                elif isinstance(event, TextMessage):
                    size += len(event.data.encode())
                    if size > MAX_MESSAGE:
                        raise GatewayError("upstream_error", 502, "stream")
                    parts.append(event.data)
                    if event.message_finished:
                        value = json.loads("".join(parts))
                        if not isinstance(value, dict):
                            raise ValueError("invalid_event")
                        return value
                else:
                    raise GatewayError("upstream_error", 502, "stream")

    async def cancel(self):
        """Close once, and make concurrent/cancelled callers join that close.

        A websocket close can be reached from both the request cancellation path
        and the provider context manager.  Returning early from the second path
        used to release the proxy lease before the network stream had actually
        closed.  Keep one shielded task and let every caller join it.
        """
        if self._close_task is None:
            self._close_task = asyncio.create_task(asyncio.wait_for(self.stream.aclose(), 5))
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        await self._close_task
        self.closed = True
        if cancelled:
            raise asyncio.CancelledError


@asynccontextmanager
async def open_codex_websocket(transport, route, proxy, account_id, access_token, deadline):
    if route.root != "https://chatgpt.com/backend-api/codex":
        raise GatewayError("egress_denied", 502, "upstream")
    for value in (account_id, access_token):
        if not value or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
            raise GatewayError("invalid_state", 502, "upstream")
    adapter, response = None, None
    response_acquired = False
    try:
        await asyncio.wait_for(transport._validate(route, proxy), max(0, deadline-time.monotonic()))
        protocol = WSConnection(ConnectionType.CLIENT)
        handshake = protocol.send(Request(host="chatgpt.com", target="/backend-api/codex/responses",
            extra_headers=[(b"authorization", ("Bearer "+access_token).encode()),
                (b"chatgpt-account-id", account_id.encode()),
                (b"openai-beta", b"responses_websockets=2026-02-06")]))
        headers = {}
        for line in handshake.split(b"\r\n")[1:]:
            if b": " in line:
                name, value = line.split(b": ", 1)
                if name.lower() != b"host":
                    headers[name.decode("ascii")] = value.decode("latin1")
        headers["Originator"] = "codex_gateway"
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise GatewayError("deadline_exceeded", 504, "upstream")
        adapter = transport.adapter or CoreTransport(transport.policy, proxy)
        call = httpx.Request("GET", route.root+"/responses", headers=headers,
            extensions={"timeout": {"connect": min(10, remaining), "read": min(60, remaining),
                                    "write": min(60, remaining), "pool": min(60, remaining)}})
        response = await asyncio.wait_for(adapter.handle_async_request(call), remaining)
        response_acquired = True
        if response.status_code in {400, 401, 403, 404, 422, 429}:
            rejected = UpstreamRejected(response.status_code, parse_retry_after(response.headers.get("retry-after")))
            rejected.received_at = time.monotonic()
            raise rejected
        if response.status_code != 101:
            raise GatewayError("upstream_error", 502, "upstream")
        protocol.receive_data(b"HTTP/1.1 101 Switching Protocols\r\n" + b"".join(
            name+b": "+value+b"\r\n" for name, value in response.headers.raw)+b"\r\n")
        if not any(isinstance(event, AcceptConnection) for event in protocol.events()):
            raise GatewayError("upstream_error", 502, "upstream")
        stream = response.extensions.get("network_stream")
        if stream is None:
            raise GatewayError("upstream_error", 502, "upstream")
        connection = CodexSocket(protocol, stream, deadline, proxy_used=proxy is not None)
        try:
            yield connection
        finally:
            await connection.cancel()
    except GatewayError:
        raise
    except (asyncio.TimeoutError, httpcore.TimeoutException, httpx.TimeoutException):
        raise GatewayError("deadline_exceeded", 504, "upstream") from None
    except (httpcore.NetworkError, httpcore.ProtocolError, httpx.HTTPError, OSError) as exc:
        raise TransportFailure(before_response=not response_acquired, proxy_used=proxy is not None,
                               request_not_transmitted=isinstance(exc, (httpx.ConnectError, httpcore.ConnectError))) from None
    except (ValueError, RemoteProtocolError):
        raise GatewayError("proxy_error" if proxy else "upstream_error", 502, "upstream") from None
    finally:
        try:
            if response is not None:
                await _account_close(response)
        finally:
            if adapter is not None and transport.adapter is None:
                await _account_close(adapter)
