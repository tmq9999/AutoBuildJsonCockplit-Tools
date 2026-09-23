import pytest


def test_kiot_key_lines_deduplicate_and_never_repr_secret():
    from autobuild_json.gateway.proxy.config import parse_kiot_keys
    keys = parse_kiot_keys(" synthetic-one\n\nsynthetic-two\nsynthetic-one ", pepper=b"p"*32)
    assert len(keys) == 2 and keys[0].fingerprint != keys[1].fingerprint
    assert "synthetic-one" not in repr(keys)
    with pytest.raises(ValueError):
        parse_kiot_keys("key with spaces", pepper=b"p"*32)


def test_proxy_selection_rejects_implicit_direct_and_conflicting_entries():
    from autobuild_json.gateway.proxy.config import ProxySelection
    from autobuild_json.models import ProxyConfig
    with pytest.raises(ValueError):
        ProxySelection("pool", runtime_entries=())
    with pytest.raises(ValueError):
        ProxySelection("direct", runtime_entries=(ProxyConfig("http://proxy.invalid:8000"),))
