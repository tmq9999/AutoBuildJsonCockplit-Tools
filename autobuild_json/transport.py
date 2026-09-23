import json
from urllib.parse import unquote, urljoin, urlsplit

from curl_cffi import CurlOpt, requests
from curl_cffi.requests.exceptions import RequestException

from .diagnostics import request_details
from .errors import FlowError

MAX_RESPONSE = 2 * 1024 * 1024
PATHS = {
    "auth.openai.com": ("/oauth/", "/api/accounts/", "/api/oauth/", "/log-in", "/mfa-challenge/", "/choose-an-account", "/sign-in-with-chatgpt/", "/consent", "/workspace", "/email-verification", "/add-phone", "/phone-verification", "/.well-known/"),
    "chatgpt.com": ("/api/auth/", "/codex/desktop-auth"),
    "sentinel.openai.com": ("/backend-api/sentinel/",),
}


def validate_url(url):
    try:
        p = urlsplit(url)
        decoded = unquote(p.path)
        if (p.scheme != "https" or p.hostname not in PATHS or p.username is not None
                or p.password is not None or p.port not in {None, 443} or p.fragment
                or any(ord(c) <= 32 for c in url) or "\\" in url
                or any(segment in {".", ".."} for segment in decoded.split("/"))):
            raise ValueError()
        if not (p.path == "/" or any(p.path.startswith(prefix) for prefix in PATHS[p.hostname])):
            raise ValueError()
    except ValueError:
        raise FlowError("AUTH_BLOCKED", "redirect") from None


class SafeTransport:
    def __init__(self, proxy, context, raw_session=None):
        self.proxy = proxy
        self.context = context
        self.raw = raw_session or requests.Session(impersonate="chrome", trust_env=False)
        # curl_cffi's trust_env is not sufficient for libcurl environment variables.
        # Options are applied last by the HTTP library, per session, never globally.
        self.raw.curl_options = dict(getattr(self.raw, "curl_options", {}))
        self.raw.curl_options.update({CurlOpt.PROXY:proxy.url if proxy else "", CurlOpt.NOPROXY:""})
        self.cookies = self.raw.cookies

    def request(self, method, url, **kwargs):
        validate_url(url)
        self.context.check()
        content = bytearray()
        problem = []

        def receive(chunk):
            try:
                self.context.check()
                if len(content) + len(chunk) > MAX_RESPONSE:
                    raise FlowError("AUTH_BLOCKED", "response_size")
                content.extend(chunk)
                return len(chunk)
            except FlowError as exc:
                problem.append(exc)
                return 0

        forwarded = {key:value for key,value in kwargs.items() if key in {"headers", "data", "json"}}
        timeout = min(float(kwargs.get("timeout", 30)), self.context.remaining_timeout())
        try:
            response = self.raw.request(method=method, url=url, **forwarded, verify=True,
                allow_redirects=False, timeout=timeout,
                proxies={"http":self.proxy.url, "https":self.proxy.url} if self.proxy else {},
                content_callback=receive)
        except Exception as exc:
            if problem:
                raise problem[0] from None
            self.context.check()
            details = {**request_details(method, url), "exception_type": type(exc).__name__,
                       "timeout_ms": round(timeout * 1000)}
            if isinstance(exc, RequestException):
                curl_code = int(exc.code)
                details["curl_code"] = curl_code
                code = "REQUEST_TIMEOUT" if curl_code == 28 else "PROXY_ERROR" if self.proxy else "NETWORK_ERROR"
            elif isinstance(exc, OSError):
                code = "PROXY_ERROR" if self.proxy else "NETWORK_ERROR"
            else:
                code = "UNEXPECTED_ERROR"
            raise FlowError(code, "http", details=details) from None
        if problem:
            raise problem[0]
        response.content = bytes(content)
        return response

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def follow(self, method, url, **kwargs):
        for _ in range(13):
            response = self.request(method, url, **kwargs)
            if response.status_code not in {301,302,303,307,308}:
                return response
            location = response.headers.get("Location", "")
            if not location:
                raise FlowError("AUTH_BLOCKED", "redirect")
            next_url = urljoin(url, location)
            if response.status_code in {307,308} and method != "GET":
                raise FlowError("AUTH_BLOCKED", "redirect")
            url, method, kwargs = next_url, "GET", {}
        raise FlowError("AUTH_BLOCKED", "redirect")

    def close(self):
        self.raw.close()


def object_json(response, code="AUTH_BLOCKED"):
    try:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError()
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        raise FlowError(code, "response") from None
