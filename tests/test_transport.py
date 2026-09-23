import pytest
from autobuild_json.transport import SafeTransport
from autobuild_json.errors import FlowError
from tests.fakes import Response

class Raw:
    def __init__(self):
        self.calls = []
        self.cookies = {}
    def request(self, **kwargs):
        self.calls.append(kwargs)
        kwargs["content_callback"](b'{"ok":true}')
        return Response()
    def close(self):
        pass

@pytest.mark.parametrize("url", ["http://auth.openai.com/", "https://evil.test/", "http://localhost:1455/auth/callback", "https://x@auth.openai.com/", "https://auth.openai.com:444/", "https://auth.openai.com/../evil", "https://auth.openai.com/foo"])
def test_rejects_url_before_request(context, url):
    raw = Raw()
    with pytest.raises(FlowError):
        SafeTransport(None, context, raw).request("GET", url)
    assert raw.calls == []

def test_forces_tls_no_redirect_and_no_env_proxy(context):
    raw = Raw()
    response = SafeTransport(None, context, raw).request("GET", "https://auth.openai.com/oauth/authorize")
    assert response.json() == {"ok":True}
    assert raw.calls[0]["verify"] is True
    assert raw.calls[0]["allow_redirects"] is False
    assert raw.calls[0]["timeout"] <= 30

def test_response_limit(context):
    class Huge(Raw):
        def request(self, **kwargs):
            kwargs["content_callback"](b'x' * (2 * 1024 * 1024 + 1))
            return Response()
    with pytest.raises(FlowError):
        SafeTransport(None, context, Huge()).request("GET", "https://auth.openai.com/oauth/authorize")


def test_actual_curl_options_override_environment_proxy_policy(context, monkeypatch):
    from curl_cffi import CurlOpt
    from curl_cffi.requests.utils import set_curl_options
    from autobuild_json.proxies import parse_proxies
    monkeypatch.setenv("HTTPS_PROXY", "http://unwanted.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "*")
    class RecordingCurl:
        def __init__(self):
            self.options = {}
        def setopt(self, option, value):
            self.options[option] = value
    for proxy in (None, parse_proxies("chosen.invalid:8080")[0]):
        transport = SafeTransport(proxy, context)
        try:
            curl = RecordingCurl()
            # Execute the installed library's option assembly, without socket I/O.
            set_curl_options(curl, "GET", "https://fixture.invalid/", params_list=[None, None],
                headers_list=[None, None], cookies_list=[None, None], proxies_list=[None, None],
                verify_list=[True, True], curl_options=transport.raw.curl_options)
            assert curl.options[CurlOpt.PROXY] == (proxy.url if proxy else "")
            assert curl.options[CurlOpt.NOPROXY] == ""
        finally:
            transport.close()
