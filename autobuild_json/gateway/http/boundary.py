from urllib.parse import parse_qsl
from collections import OrderedDict, deque
import time
import asyncio

from starlette.responses import JSONResponse


class GatewayBoundary:
    def __init__(self, app, allowed_hosts, *, ingress_rpm=600, max_body=16*1024*1024):
        self.app, self.allowed_hosts = app, frozenset(allowed_hosts)
        self.ingress_rpm, self.max_body = ingress_rpm, max_body
        self.clients = OrderedDict()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        pairs = scope["headers"]
        headers = dict(pairs)
        host = headers.get(b"host", b"").decode("latin1")
        origin = headers.get(b"origin", b"").decode("latin1")
        async def reject(status, code):
            await JSONResponse({"error": {"code": code, "message": code}}, status_code=status)(scope, receive, send)
        if host not in self.allowed_hosts or sum(k == b"host" for k, _ in pairs) != 1:
            return await reject(400, "invalid_host")
        peer = (scope.get("client") or ("unknown", 0))[0]
        now = time.monotonic()
        queue = self.clients.setdefault(peer, deque())
        self.clients.move_to_end(peer)
        while queue and queue[0] <= now-60:
            queue.popleft()
        if len(queue) >= self.ingress_rpm:
            return await reject(429, "rate_limited")
        queue.append(now)
        while len(self.clients) > 10000:
            self.clients.popitem(last=False)
        if origin and origin != scope.get("scheme", "http")+"://"+host:
            return await reject(403, "invalid_origin")
        if scope["method"] in {"POST", "PUT", "PATCH"} and headers.get(b"content-type", b"").split(b";", 1)[0].strip() != b"application/json":
            return await reject(415, "json_required")
        try:
            query = parse_qsl(scope.get("query_string", b"").decode("ascii"), keep_blank_values=True)
        except (ValueError, UnicodeError):
            return await reject(400, "invalid_request")
        if any(k not in {"alt", "limit", "after_id", "before_id", "pageSize", "pageToken"} for k, _ in query):
            return await reject(400, "query_not_allowed")
        body = bytearray()
        body_deadline = time.monotonic()+10
        while True:
            try:
                message = await asyncio.wait_for(receive(), max(0.001, body_deadline-time.monotonic()))
            except asyncio.TimeoutError:
                return await reject(408, "body_timeout")
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_body:
                return await reject(413, "input_too_large")
            if not message.get("more_body", False):
                break
        replayed = False
        disconnected=asyncio.Event()
        parent=asyncio.current_task()
        scope['gateway_disconnect_guard']=True
        async def watch_disconnect():
            while True:
                if (await receive())['type']=='http.disconnect':
                    disconnected.set()
                    parent.cancel()
                    return
        watcher=asyncio.create_task(watch_disconnect())
        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            await disconnected.wait()
            return {'type':'http.disconnect'}
        try:
            await self.app(scope, replay, send)
        except asyncio.CancelledError:
            if not disconnected.is_set():
                raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher,return_exceptions=True)
