"""In-memory ASGI and RFC6455 peers; never connects to an external provider."""
import asyncio
from contextlib import asynccontextmanager
import json

import httpx
from wsproto import ConnectionType, WSConnection
from wsproto.events import AcceptConnection, Request, TextMessage


class SocketStream:
    def __init__(self, protocol, events=()):
        self.protocol, self.events = protocol, events
        self.incoming = asyncio.Queue()
        self.messages, self.received = [], []
        self.closed = False
        self.started = asyncio.Event()

    async def write(self, data, timeout=None):
        self.protocol.receive_data(data)
        for event in self.protocol.events():
            self.received.append(event)
            if isinstance(event, TextMessage):
                self.messages.append(json.loads(event.data))
                self.started.set()
                # Coalesced frames exercise the receiver's pending-event buffer.
                if self.events:
                    self.incoming.put_nowait(b"".join(self.protocol.send(TextMessage(data=json.dumps(value)))
                                                     for value in self.events))

    async def read(self, max_bytes, timeout=None):
        return await self.incoming.get()

    async def aclose(self):
        self.closed = True


class SocketTransport(httpx.AsyncBaseTransport):
    def __init__(self, events=(), *, status=101, invalid_accept=False):
        self.events, self.status, self.invalid_accept = events, status, invalid_accept
        self.requests, self.streams, self.responses = [], [], []
        self.closed = False

    async def handle_async_request(self, request):
        self.requests.append(request)
        if self.status != 101:
            response = httpx.Response(self.status, headers={"retry-after": "10"})
        else:
            protocol = WSConnection(ConnectionType.SERVER)
            protocol.receive_data(b"GET " + request.url.raw_path + b" HTTP/1.1\r\n" + b"".join(
                key + b": " + value + b"\r\n" for key, value in request.headers.raw) + b"\r\n")
            assert any(isinstance(event, Request) for event in protocol.events())
            wire = protocol.send(AcceptConnection())
            headers = [tuple(line.split(b": ", 1)) for line in wire.split(b"\r\n")[1:] if b": " in line]
            if self.invalid_accept:
                headers = [(key, b"invalid" if key.lower() == b"sec-websocket-accept" else value)
                           for key, value in headers]
            stream = SocketStream(protocol, self.events)
            self.streams.append(stream)
            response = httpx.Response(101, headers=headers, extensions={"network_stream": stream})
        self.responses.append(response)
        return response

    async def aclose(self):
        self.closed = True


class ASGISocket:
    def __init__(self):
        self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue()

    async def send_json(self, value):
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps(value)})

    async def receive(self):
        return await asyncio.wait_for(self.outgoing.get(), 10)

    async def receive_json(self):
        message = await self.receive()
        assert message["type"] == "websocket.send", message
        return json.loads(message["text"])

    async def turn(self, value):
        await self.send_json(value)
        result = []
        while True:
            event = await self.receive_json()
            result.append(event)
            if event["type"] in {"response.completed", "response.incomplete", "response.failed", "error"}:
                return result


@asynccontextmanager
async def asgi_socket(app, *, path="/v1/responses", headers=(), query=b""):
    socket = ASGISocket()
    scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "scheme": "ws", "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": query, "headers": list(headers), "subprotocols": [],
             "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8788)}
    await socket.incoming.put({"type": "websocket.connect"})
    task = asyncio.create_task(app(scope, socket.incoming.get, socket.outgoing.put))
    try:
        yield socket
    finally:
        await socket.incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, 10)
