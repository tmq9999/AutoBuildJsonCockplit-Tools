import pytest
from pydantic import ValidationError

from autobuild_json.gateway.settings import ServiceSettings


@pytest.mark.parametrize('trusted', [('*',), ('127.0.0.1,*',), ('0.0.0.0/0',), ('::/0',), ('invalid-host',)])
def test_forwarded_headers_require_explicit_trusted_addresses(trusted):
    with pytest.raises(ValidationError):
        ServiceSettings(host='0.0.0.0', tls_proxy_configured=True,
                        allowed_hosts=('gateway.example',), trusted_proxy_ips=trusted)


def test_explicit_proxy_ip_and_network_are_supported():
    settings = ServiceSettings(trusted_proxy_ips=('127.0.0.1', '10.10.0.0/24', '::1'))
    assert settings.trusted_proxy_ips == ('127.0.0.1', '10.10.0.0/24', '::1')
