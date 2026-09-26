"""Local-only admin sessions and a strict HTTP boundary."""
import json
import secrets
import threading
import time
import uuid
import re
from collections import deque
from urllib.parse import parse_qsl

from fastapi import HTTPException
from starlette.responses import JSONResponse

from .errors import FlowError
from .results import atomic_json, private_directory

MAX_BODY = 32 * 1024 * 1024


def prepare_settings(settings):
    private_directory(settings.data_dir)
    path = settings.data_dir / "local-config.json"
    try:
        if path.is_symlink():
            raise ValueError()
        data = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError()
        if not settings.installation_id:
            settings.installation_id = data.get("installation_id") or str(uuid.uuid4())
        if not settings.admin_token.get_secret_value():
            from pydantic import SecretStr
            settings.admin_token = SecretStr(data.get("admin_token") or secrets.token_urlsafe(32))
        if len(settings.admin_token.get_secret_value()) < 12:
            raise ValueError()
        uuid.UUID(settings.installation_id)
        desired = {"installation_id":settings.installation_id,
            "admin_token":settings.admin_token.get_secret_value()}
        if data != desired:
            atomic_json(path, desired)
    except (ValueError, OSError):
        raise FlowError("CONFIGURATION_ERROR", "local_config") from None
    return settings


class AdminSessions:
    def __init__(self, token, clock=time.monotonic):
        self._token = token
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions = {}
        self._failures = deque()

    def login(self, token, old_sid=None):
        with self._lock:
            now = self._clock()
            while self._failures and self._failures[0] <= now - 60:
                self._failures.popleft()
            if len(self._failures) >= 5:
                raise HTTPException(429, "LOGIN_RATE_LIMITED")
            if not secrets.compare_digest(token.encode(), self._token.encode()):
                self._failures.append(now)
                raise HTTPException(401, "UNAUTHORIZED")
            self._sessions = {k:v for k,v in self._sessions.items() if v[0] > now}
            self._sessions.pop(old_sid, None)
            if len(self._sessions) >= 100:
                raise HTTPException(429, "SESSION_LIMIT")
            sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self._sessions[sid] = (now + 28800, csrf)
            return sid, csrf

    def require(self, request):
        sid = request.cookies.get("autobuild_session")
        with self._lock:
            entry = self._sessions.get(sid)
            if not entry or entry[0] <= self._clock():
                self._sessions.pop(sid, None)
                raise HTTPException(401, "UNAUTHORIZED")
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                csrf = request.headers.get("X-CSRF-Token", "")
                if not secrets.compare_digest(csrf.encode(), entry[1].encode()):
                    raise HTTPException(403, "CSRF_REJECTED")
            return sid

    def csrf(self, sid):
        with self._lock:
            return self._sessions[sid][1]

    def logout(self, sid):
        with self._lock:
            self._sessions.pop(sid, None)


class BoundaryMiddleware:
    def __init__(self, app, port):
        self.app = app
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        host = headers.get(b"host", b"").decode("latin1")
        async def reject(status, code):
            await JSONResponse({"error":code}, status_code=status, headers={"Cache-Control":"no-store"})(scope, receive, send)
        if host not in self.hosts or sum(k == b"host" for k,v in scope["headers"]) != 1:
            return await reject(400, "INVALID_HOST")
        origin = headers.get(b"origin", b"").decode("latin1")
        expected_origin = scope.get("scheme", "http") + "://" + host
        mutation = scope["method"] not in {"GET", "HEAD", "OPTIONS"}
        if (sum(key == b"origin" for key, _ in scope["headers"]) > 1
                or (origin and origin != expected_origin) or (mutation and origin != expected_origin)):
            return await reject(403, "INVALID_ORIGIN")
        if scope.get("query_string") and not self._catalog_query(scope):
            return await reject(400, "QUERY_NOT_ALLOWED")
        if mutation and scope["method"] != "DELETE" and headers.get(b"content-type", b"").split(b";")[0].strip() != b"application/json":
            return await reject(415, "JSON_REQUIRED")
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > MAX_BODY:
                return await reject(413, "INPUT_TOO_LARGE")
            if not message.get("more_body", False):
                break
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type":"http.request", "body":bytes(body), "more_body":False}
            return await receive()
        async def secured_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control",b"no-store"), (b"x-content-type-options",b"nosniff"),
                    (b"referrer-policy",b"no-referrer"),
                    (b"content-security-policy",b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                ]
            await send(message)
        await self.app(scope, replay, secured_send)

    @staticmethod
    def _catalog_query(scope):
        """Permit only the private catalog's read-only, non-secret query contract."""
        if scope["method"] != "GET":
            return False
        path = scope.get("path", "")
        if path == "/api/service/oauth-accounts":
            allowed = {"limit", "after", "page", "q", "status"}
        elif path in {"/api/service/usage/accounts", "/api/service/usage/requests"}:
            allowed = {"credential_id", "model_id", "from", "to", "limit", "after"}
        elif path == "/api/service/catalog/sources":
            allowed = {"provider_id"}
        elif re.fullmatch(r"/api/service/catalog/sources/[0-9a-fA-F-]{36}/entries", path):
            allowed = {"cursor", "limit", "diff"}
        elif re.fullmatch(r"/api/service/catalog/sources/[0-9a-fA-F-]{36}/probe-quote", path):
            allowed = {"binding_id"}
        else:
            return False
        raw = scope["query_string"]
        if len(raw) > 16384:
            return False
        try:
            pairs = parse_qsl(raw.decode("ascii"), keep_blank_values=True, strict_parsing=True,
                              errors="strict", max_num_fields=len(allowed))
            return bool(pairs) and len({key for key, _ in pairs}) == len(pairs) and all(
                key in allowed and not any(ord(char) < 32 or ord(char) == 127 for char in value)
                for key, value in pairs)
        except (ValueError, UnicodeError):
            return False
