from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from curl_cffi.requests import Cookies

from autobuild_json.checklive_adapter import AuthTransport, guarded_auth, load_checklive
from autobuild_json.errors import FlowError
from autobuild_json.login_bootstrap import initialize_login
from autobuild_json.proxies import parse_proxies
from tests.fakes import Response


PROVIDERS = {"openai": {"id": "openai", "name": "openai", "type": "oauth",
    "signinUrl": "https://chatgpt.com/api/auth/signin/openai",
    "callbackUrl": "https://chatgpt.com/api/auth/callback/openai"}}
AUTHORIZE = "https://auth.openai.com/api/accounts/authorize?state=server-generated"


class BootstrapRaw:
    def __init__(self, responses=None):
        self.cookies = Cookies()
        self.calls = []
        self.responses = responses if responses is not None else [
            Response(PROVIDERS), Response({"csrfToken": "fake+csrf&token"}), Response({"url": AUTHORIZE})]

    def request(self, **kwargs):
        self.calls.append({**kwargs, "cookies": dict(self.cookies.items())})
        path = urlsplit(kwargs["url"]).path
        if path == "/api/auth/providers":
            self.cookies.set("provider-cookie", "from-server", domain="chatgpt.com", secure=True)
        if path == "/api/auth/csrf":
            self.cookies.set("__Host-next-auth.csrf-token", "server-csrf-cookie", secure=True)
        response = self.responses.pop(0)
        kwargs["content_callback"](response.content)
        return response

    def close(self):
        pass


def setup_auth(context, raw, proxy=None):
    module = load_checklive(Path(".deps/Check-Account-ChatGPT"))
    return guarded_auth(module, AuthTransport(proxy, context, raw))


def test_capture_order_uuid_correlation_cookie_jar_and_encoded_form(context):
    raw = BootstrapRaw()
    proxy = parse_proxies("http://user:secret@proxy.test:8080")[0]
    auth = setup_auth(context, raw, proxy)
    stages = []
    assert initialize_login(auth, "user+test@example.com", stages.append) == AUTHORIZE
    assert [(c["method"], urlsplit(c["url"]).path) for c in raw.calls] == [
        ("GET", "/api/auth/providers"), ("GET", "/api/auth/csrf"), ("POST", "/api/auth/signin/openai")]
    assert stages == ["providers", "csrf", "signin"]
    signin = raw.calls[-1]
    query = parse_qs(urlsplit(signin["url"]).query)
    assert query["prompt"] == ["login"]
    assert query["screen_hint"] == ["login_or_signup"]
    assert query["login_hint"] == ["user+test@example.com"]
    device, logging_id = query["ext-oai-did"][0], query["auth_session_logging_id"][0]
    assert UUID(device).version == UUID(logging_id).version == 4
    assert device != logging_id
    assert auth.device_id == device
    for call in raw.calls:
        assert "cookie" not in {key.lower() for key in call["headers"]}
        assert call["cookies"]["oai-did"] == device
        assert call["cookies"]["oai-asli"] == logging_id
        referer = urlsplit(call["headers"]["referer"])
        assert referer.scheme == "https" and referer.netloc == "chatgpt.com"
        assert referer.path == "/auth/login_with"
        ref_query = parse_qs(referer.query)
        assert ref_query["callback_path"] == ["/"]
        assert ref_query["auth_session_logging_id"] == [logging_id]
        assert ref_query["ext-oai-did"] == [device]
        assert ref_query["login_hint"] == ["user+test@example.com"]
        assert call["proxies"]["https"] == proxy.url
        assert call["allow_redirects"] is False
    assert raw.calls[1]["cookies"]["provider-cookie"] == "from-server"
    assert signin["cookies"]["__Host-next-auth.csrf-token"] == "server-csrf-cookie"
    assert parse_qs(signin["data"]) == {"callbackUrl": ["https://chatgpt.com/"],
        "csrfToken": ["fake+csrf&token"], "json": ["true"]}
    assert signin["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert not any(call.get("data") or call.get("json") for call in raw.calls[:2])


def test_new_attempt_has_new_uuid_and_no_shared_cookies(context):
    ids = []
    for _ in range(2):
        raw = BootstrapRaw()
        initialize_login(setup_auth(context, raw), "u@example.com", lambda stage: None)
        params = parse_qs(urlsplit(raw.calls[-1]["url"]).query)
        ids.extend([params["ext-oai-did"][0], params["auth_session_logging_id"][0]])
        assert set(raw.calls[0]["cookies"]) == {"oai-did", "oai-asli"}
    assert len(set(ids)) == 4


@pytest.mark.parametrize("payload", [{}, {"openai": None}, {"openai": {"id":"openai-dev"}},
    {"openai": {**PROVIDERS["openai"], "signinUrl":"https://evil.test/collect"}}])
def test_provider_response_must_select_fixed_openai_provider(context, payload):
    raw = BootstrapRaw([Response(payload)])
    with pytest.raises(FlowError) as error:
        initialize_login(setup_auth(context, raw), "u@example.com", lambda stage: None)
    assert error.value.code == "AUTH_BOOTSTRAP_ERROR"
    assert error.value.stage == "providers"
    assert len(raw.calls) == 1


@pytest.mark.parametrize("payload", [{}, {"csrfToken": ""}, {"csrfToken": None}, {"csrfToken": 123}])
def test_missing_or_invalid_csrf_stops_before_signin(context, payload):
    raw = BootstrapRaw([Response(PROVIDERS), Response(payload)])
    with pytest.raises(FlowError) as error:
        initialize_login(setup_auth(context, raw), "u@example.com", lambda stage: None)
    assert error.value.code == "AUTH_BOOTSTRAP_ERROR"
    assert error.value.stage == "csrf"
    assert len(raw.calls) == 2


@pytest.mark.parametrize("url", ["https://evil.test/auth", "http://auth.openai.com/api/accounts/authorize",
    "https://auth.openai.com@evil.test/api/accounts/authorize", "https://auth.openai.com:444/api/accounts/authorize",
    "https://auth.openai.com/api/accounts/password/verify", "", None])
def test_signin_target_is_validated_before_credentials_are_sent(context, url):
    raw = BootstrapRaw([Response(PROVIDERS), Response({"csrfToken":"fake"}), Response({"url":url})])
    with pytest.raises(FlowError) as error:
        initialize_login(setup_auth(context, raw), "u@example.com", lambda stage: None)
    assert error.value.code == "AUTH_BOOTSTRAP_ERROR"
    assert error.value.stage == "signin"
    assert len(raw.calls) == 3


def test_providers_redirect_does_not_fall_back_to_homepage(context):
    raw = BootstrapRaw([Response(status=302, location="https://chatgpt.com/")])
    with pytest.raises(FlowError) as error:
        initialize_login(setup_auth(context, raw), "u@example.com", lambda stage: None)
    assert error.value.code == "AUTH_BOOTSTRAP_ERROR"
    assert len(raw.calls) == 1
