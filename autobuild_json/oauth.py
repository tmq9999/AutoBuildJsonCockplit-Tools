import base64
import hashlib
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit

from .errors import FlowError
from .input_parser import valid_email
from .models import ProxyConfig

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
ISSUER = "https://auth.openai.com"


@dataclass(frozen=True)
class OAuthSession:
    session_id: str
    desktop_url: str = field(repr=False)
    authorize_url: str = field(repr=False)
    expires_at: float


@dataclass(frozen=True)
class AuthorizationGrant:
    expected_email: str
    code: str = field(repr=False)
    verifier: str = field(repr=False)
    proxy: ProxyConfig | None = field(repr=False)
    redirect_uri: str = REDIRECT_URI


def is_callback(url: str) -> bool:
    try:
        p = urlsplit(url)
        return (p.scheme == "http" and p.netloc == "localhost:1455" and p.path == "/auth/callback"
                and not p.fragment and not any(ord(c) <= 32 for c in url))
    except ValueError:
        return False


class OAuthSessions:
    def __init__(self, settings, clock=time.monotonic):
        self.settings = settings
        self.clock = clock
        self._lock = threading.Lock()
        self._pending = {}
        self.installation_id = settings.installation_id or str(uuid.uuid4())

    def create(self, expected_email: str, proxy: ProxyConfig | None) -> OAuthSession:
        if not valid_email(expected_email):
            raise FlowError("INVALID_INPUT", "oauth_create")
        with self._lock:
            now = self.clock()
            self._pending = {k:v for k,v in self._pending.items() if v[0] > now}
            if len(self._pending) >= 100:
                raise FlowError("RATE_LIMITED", "oauth_create")
            verifier, state, sid = secrets.token_urlsafe(48), secrets.token_urlsafe(32), str(uuid.uuid4())
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            params = {
                "response_type":"code", "client_id":CLIENT_ID, "redirect_uri":REDIRECT_URI,
                "scope":"openid profile email offline_access api.connectors.read api.connectors.invoke",
                "code_challenge":challenge, "code_challenge_method":"S256",
                "id_token_add_organizations":"true", "codex_cli_simplified_flow":"true",
                "codex_streamlined_login":"true", "state":state, "originator":"Codex Desktop",
                "codex_app_version":self.settings.app_version,
                "source_surface_stable_id":self.installation_id, "codex_origin_stable_id":self.installation_id,
            }
            authorize = ISSUER + "/oauth/authorize?" + urlencode(params)
            desktop = "https://chatgpt.com/codex/desktop-auth?" + urlencode({
                "authorize_url":authorize, "codex_streamlined_login":"true", "no_universal_links":"1"})
            self._pending[sid] = (now + 600, state, verifier, expected_email, proxy)
            return OAuthSession(sid, desktop, authorize, now + 600)

    def claim(self, session_id: str, callback_url: str) -> AuthorizationGrant:
        if len(callback_url) > 16_384 or not is_callback(callback_url):
            raise FlowError("INVALID_STATE", "callback")
        try:
            values = parse_qs(urlsplit(callback_url).query, keep_blank_values=True, strict_parsing=True, max_num_fields=10)
        except ValueError:
            raise FlowError("INVALID_STATE", "callback") from None
        if any(len(v) != 1 or not v[0] for v in values.values()) or not values.get("state"):
            raise FlowError("INVALID_STATE", "callback")
        if bool(values.get("code")) == bool(values.get("error")):
            raise FlowError("INVALID_STATE", "callback")
        with self._lock:
            entry = self._pending.get(session_id)
            if not entry or entry[0] <= self.clock():
                self._pending.pop(session_id, None)
                raise FlowError("INVALID_STATE", "callback")
            expiry, state, verifier, email, proxy = entry
            if not secrets.compare_digest(values["state"][0].encode(), state.encode()):
                raise FlowError("INVALID_STATE", "callback")
            del self._pending[session_id]
            if "error" in values:
                raise FlowError("OAUTH_DENIED", "callback")
            return AuthorizationGrant(email, values["code"][0], verifier, proxy)

    def discard(self, session_id):
        with self._lock:
            self._pending.pop(session_id, None)
