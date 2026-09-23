import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit
import pytest
from autobuild_json.oauth import OAuthSessions
from autobuild_json.errors import FlowError

def callback(item, suffix=""):
    state = parse_qs(urlsplit(item.authorize_url).query)["state"][0]
    return "http://localhost:1455/auth/callback?code=c&state=" + state + suffix

def test_url_pkce_and_single_use(settings):
    sessions = OAuthSessions(settings)
    item = sessions.create("u@example.com", None)
    outer = parse_qs(urlsplit(item.desktop_url).query)
    inner = parse_qs(urlsplit(outer["authorize_url"][0]).query)
    assert outer["no_universal_links"] == ["1"]
    assert inner["redirect_uri"] == ["http://localhost:1455/auth/callback"]
    grant = sessions.claim(item.session_id, callback(item))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(grant.verifier.encode()).digest()).rstrip(b"=").decode()
    assert inner["code_challenge"] == [challenge]
    assert inner["source_surface_stable_id"] == inner["codex_origin_stable_id"]
    with pytest.raises(FlowError):
        sessions.claim(item.session_id, callback(item))

@pytest.mark.parametrize("change", [lambda s:s.replace("localhost", "evil.test"), lambda s:s+"&state=other", lambda s:s+"&code=other", lambda s:s.replace("1455", "1555"), lambda s:s.replace("/auth/callback", "/other"), lambda s:s+"#x", lambda s:s.replace("localhost", "x@localhost")])
def test_bad_callback_does_not_consume(settings, change):
    sessions = OAuthSessions(settings)
    item = sessions.create("u@example.com", None)
    with pytest.raises(FlowError):
        sessions.claim(item.session_id, change(callback(item)))
    assert sessions.claim(item.session_id, callback(item)).code == "c"

def test_expiry_capacity_and_uniqueness(settings):
    now = [0]
    sessions = OAuthSessions(settings, clock=lambda: now[0])
    items = [sessions.create("u@example.com", None) for _ in range(100)]
    assert len({i.authorize_url for i in items}) == 100
    with pytest.raises(FlowError):
        sessions.create("u@example.com", None)
    now[0] = 601
    with pytest.raises(FlowError):
        sessions.claim(items[0].session_id, callback(items[0]))
    sessions.create("u@example.com", None)

def test_atomic_claim(settings):
    sessions = OAuthSessions(settings)
    item = sessions.create("u@example.com", None)
    def consume(_):
        try:
            sessions.claim(item.session_id, callback(item))
            return True
        except FlowError:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(consume, range(2))) == 1
