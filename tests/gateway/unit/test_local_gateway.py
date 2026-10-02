import json
import subprocess
import sys

import pytest

from scripts import local_gateway


def test_loopback_environment_remains_default():
    environment = local_gateway.gateway_bind_environment("127.0.0.1")
    assert environment == {
        "AUTOBUILD_GATEWAY_HOST": "127.0.0.1",
        "AUTOBUILD_GATEWAY_PORT": "8788",
        "AUTOBUILD_GATEWAY_ALLOWED_HOSTS": '["127.0.0.1:8788", "localhost:8788"]',
        "AUTOBUILD_GATEWAY_ALLOW_INSECURE_LAN": "false",
    }


def test_lan_environment_enables_only_exact_gateway_host():
    environment = local_gateway.gateway_bind_environment("192.168.134.128")
    assert environment == {
        "AUTOBUILD_GATEWAY_HOST": "192.168.134.128",
        "AUTOBUILD_GATEWAY_PORT": "8788",
        "AUTOBUILD_GATEWAY_ALLOWED_HOSTS": '["192.168.134.128:8788"]',
        "AUTOBUILD_GATEWAY_ALLOW_INSECURE_LAN": "true",
    }
    from autobuild_json.gateway.settings import ServiceSettings
    settings = ServiceSettings(host=environment["AUTOBUILD_GATEWAY_HOST"],
                               allow_insecure_lan=True,
                               allowed_hosts=json.loads(environment["AUTOBUILD_GATEWAY_ALLOWED_HOSTS"]),
                               _env_file=None)
    assert settings.trusted_proxy_ips == ()


@pytest.mark.parametrize("host", ["0.0.0.0", "8.8.8.8", "169.254.1.2", "::", "lan.example"])
def test_runner_rejects_unsafe_bind_before_starting_services(host):
    with pytest.raises(ValueError, match="private IPv4"):
        local_gateway.gateway_bind_environment(host)


def test_runner_cli_rejects_unsafe_host_before_accessing_postgres(tmp_path):
    result = subprocess.run(
        [sys.executable, "scripts/local_gateway.py", "--gateway-host", "0.0.0.0",
         "--postgres-bin", str(tmp_path / "missing-postgres")],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
    assert "private IPv4" in result.stderr
