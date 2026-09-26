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

    def _admit_peer(self, scope):
        peer = (scope.get("client") or ("unknown", 0))[0]
        now = time.monotonic()
        queue = self.clients.setdefault(peer, deque())
        self.clients.move_to_end(peer)
        while queue and queue[0] <= now - 60:
            queue.popleft()
        allowed = len(queue) < self.ingress_rpm
        if allowed:
            queue.append(now)
        while len(self.clients) > 10000:
            self.clients.popitem(last=False)
        return allowed

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            pairs = scope["headers"]
            headers = dict(pairs)
            host = headers.get(b"host", b"").decode("latin1")
            origin = headers.get(b"origin", b"").decode("latin1")
            scheme = "https" if scope.get("scheme") == "wss" else "http"

            async def close(code):
                await send({"type": "websocket.close", "code": code})

            if host not in self.allowed_hosts or sum(k == b"host" for k, _ in pairs) != 1:
                return await close(1008)
            if sum(k == b"origin" for k, _ in pairs) > 1 or origin and origin != f"{scheme}://{host}":
                return await close(1008)
            if not self._admit_peer(scope):
                return await close(1013)
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        pairs = scope["headers"]
        headers = dict(pairs)
        host = headers.get(b"host", b"").decode("latin1")
        origin = headers.get(b"origin", b"").decode("latin1")
        async def secured_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (key, value) for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ] + [(b"cache-control", b"no-store")]
            await send(message)
        async def reject(status, code):
            await JSONResponse({"error": {"code": code, "message": code}}, status_code=status)(scope, receive, secured_send)
        if scope['path'].startswith('/backend-api/codex/') and scope['path'] not in {
                '/backend-api/codex/responses', '/backend-api/codex/responses/compact', '/backend-api/codex/models'}:
            return await reject(404, 'not_found')
        if host not in self.allowed_hosts or sum(k == b"host" for k, _ in pairs) != 1:
            return await reject(400, "invalid_host")
        if not self._admit_peer(scope):
            return await reject(429, "rate_limited")
        if sum(k == b"origin" for k, _ in pairs) > 1 or origin and origin != scope.get("scheme", "http")+"://"+host:
            return await reject(403, "invalid_origin")
        content_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip()
        multipart_images_edit = scope["path"] == "/v1/images/edits" and content_type == b"multipart/form-data"
        if scope["method"] in {"POST", "PUT", "PATCH"} and content_type != b"application/json" and not multipart_images_edit:
            return await reject(415, "json_required")
        try:
            query = parse_qsl(scope.get("query_string", b"").decode("ascii"), keep_blank_values=True)
        except (ValueError, UnicodeError):
            return await reject(400, "invalid_request")
        model_query = {"client_version"} if scope['path'] in {'/models', '/v1/models', '/backend-api/codex/models'} else set()
        if any(k not in {"alt", "limit", "after_id", "before_id", "pageSize", "pageToken"} | model_query for k, _ in query):
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
            await self.app(scope, replay, secured_send)
        except asyncio.CancelledError:
            if not disconnected.is_set():
                raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher,return_exceptions=True)
