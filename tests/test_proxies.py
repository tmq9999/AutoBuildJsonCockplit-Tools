import pytest
from autobuild_json.proxies import parse_proxies
from autobuild_json.errors import FlowError

@pytest.mark.parametrize("raw,want", [
    ("host.test:8080", "http://host.test:8080"),
    ("http|host.test|8080|a@b|p%ss", "http://a%40b:p%25ss@host.test:8080"),
    ("socks5:host.test:1080:u:p", "socks5://u:p@host.test:1080"),
    ("http://a%40b:p%25ss@host.test:8080", "http://a%40b:p%25ss@host.test:8080"),
])
def test_proxy_formats(raw, want):
    proxy = parse_proxies(raw)[0]
    assert proxy.url == want
    assert "p%ss" not in repr(proxy)

@pytest.mark.parametrize("raw", ["host:0", "host:65536", "file://host:33", "http://host:5/path", "host:no", "", "http://:5"])
def test_invalid_proxies(raw):
    if not raw:
        assert parse_proxies(raw) == []
    else:
        with pytest.raises(FlowError):
            parse_proxies(raw)

def test_deduplicates_proxy_identity():
    assert len(parse_proxies("host:8080\nhttp://host:8080")) == 1
