"""ChatGPT web-auth initialization without a homepage warm-up request.

The browser capture specifies the request contract, not reusable credentials:
generate correlation IDs locally and obtain CSRF/cookies from this session only.
"""
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from .errors import FlowError
from .transport import validate_url

CHATGPT = "https://chatgpt.com"
SIGNIN = CHATGPT + "/api/auth/signin/openai"
CALLBACK = CHATGPT + "/api/auth/callback/openai"


def _payload(response, stage):
    try:
        if response.status_code != 200:
            raise ValueError()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError()
        return payload
    except (ValueError, TypeError):
        raise FlowError("AUTH_BOOTSTRAP_ERROR", stage) from None


def initialize_login(auth, email, on_stage):
    """Return the server-issued authorize URL, retaining one cookie jar/proxy."""
    device_id, logging_id = str(uuid4()), str(uuid4())
    auth.device_id = device_id
    auth.session.cookies.set("oai-did", device_id, domain="chatgpt.com", path="/", secure=True)
    auth.session.cookies.set("oai-asli", logging_id, domain="chatgpt.com", path="/", secure=True)
    correlation = {
        "screen_hint": "login_or_signup",
        "login_hint": email,
        "auth_session_logging_id": logging_id,
        "ext-oai-did": device_id,
    }
    referer = CHATGPT + "/auth/login_with?" + urlencode({"callback_path": "/", **correlation})
    headers = auth._common_headers(referer)
    headers.update({"accept": "*/*", "cache-control": "no-cache", "pragma": "no-cache", "priority": "u=1, i"})

    on_stage("providers")
    providers = _payload(auth._request("GET", CHATGPT + "/api/auth/providers",
        headers=headers, allow_redirects=False), "providers")
    provider = providers.get("openai")
    if (not isinstance(provider, dict) or provider.get("id") != "openai"
            or provider.get("type") != "oauth" or provider.get("signinUrl") != SIGNIN
            or provider.get("callbackUrl") != CALLBACK):
        raise FlowError("AUTH_BOOTSTRAP_ERROR", "providers")

    on_stage("csrf")
    payload = _payload(auth._request("GET", CHATGPT + "/api/auth/csrf",
        headers=headers, allow_redirects=False), "csrf")
    csrf = payload.get("csrfToken")
    if not isinstance(csrf, str) or not csrf.strip() or len(csrf) > 4096:
        raise FlowError("AUTH_BOOTSTRAP_ERROR", "csrf")

    on_stage("signin")
    payload = _payload(auth._request("POST", SIGNIN + "?" + urlencode({"prompt": "login", **correlation}),
        headers={**headers, "content-type": "application/x-www-form-urlencoded", "origin": CHATGPT},
        data=urlencode({"callbackUrl": CHATGPT + "/", "csrfToken": csrf, "json": "true"}),
        allow_redirects=False), "signin")
    url = payload.get("url")
    try:
        if not isinstance(url, str) or len(url) > 16384:
            raise ValueError()
        validate_url(url)
        parsed = urlsplit(url)
        if parsed.hostname != "auth.openai.com" or parsed.path not in {
            "/api/accounts/authorize", "/oauth/authorize", "/authorize", "/api/oauth/oauth2/auth",
        }:
            raise ValueError()
    except (ValueError, FlowError):
        raise FlowError("AUTH_BOOTSTRAP_ERROR", "signin") from None
    return url
