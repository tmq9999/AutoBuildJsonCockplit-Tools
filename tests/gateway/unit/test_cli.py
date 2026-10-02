import subprocess
import sys

import pytest


def test_gateway_cli_help_is_separate_from_legacy_cli():
    result = subprocess.run([sys.executable, "-m", "autobuild_json.gateway", "--help"], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "serve" in result.stdout and "migrate" in result.stdout


def test_gateway_init_refuses_overwrite_and_creates_private_keyring(tmp_path):
    from autobuild_json.gateway.__main__ import initialize_keyring
    from autobuild_json.gateway.secrets import Vault
    path = tmp_path / "keyring.json"
    initialize_keyring(path)
    first = path.read_bytes()
    assert len(Vault.from_file(path).derive_key("client_keys", "client-key-hmac")) == 32
    try:
        initialize_keyring(path)
    except FileExistsError:
        pass
    else:
        raise AssertionError("must not overwrite operator keys")
    assert path.read_bytes() == first


def test_service_public_bind_requires_explicit_tls_boundary(tmp_path):
    import pytest
    from autobuild_json.gateway.settings import ServiceSettings
    with pytest.raises(ValueError):
        ServiceSettings(enabled=True, host="0.0.0.0", database_url="postgresql+psycopg://u:p@localhost/service",
                        master_key_file=tmp_path/"key", _env_file=None)


def test_service_private_lan_bind_requires_explicit_opt_in(tmp_path):
    from autobuild_json.gateway.settings import ServiceSettings
    database_url = "postgresql+psycopg://u:p@localhost/service"
    key_file = tmp_path / "key"
    with pytest.raises(ValueError):
        ServiceSettings(enabled=True, host="192.168.134.128", database_url=database_url,
                        master_key_file=key_file, allowed_hosts=("192.168.134.128:8788",), _env_file=None)
    settings = ServiceSettings(enabled=True, host="192.168.134.128", allow_insecure_lan=True,
                               database_url=database_url, master_key_file=key_file,
                               allowed_hosts=("192.168.134.128:8788",), _env_file=None)
    assert settings.host == "192.168.134.128"


@pytest.mark.parametrize("host", [
    "0.0.0.0", "8.8.8.8", "169.254.1.2", "192.0.0.1", "100.64.0.1",
    "::", "fd00::1", "lan.example", "*",
])
def test_service_insecure_lan_opt_in_rejects_non_rfc1918_hosts(tmp_path, host):
    from autobuild_json.gateway.settings import ServiceSettings
    database_url = "postgresql+psycopg://u:p@localhost/service"
    key_file = tmp_path / "key"
    with pytest.raises(ValueError):
        ServiceSettings(enabled=True, host=host, allow_insecure_lan=True,
                        database_url=database_url, master_key_file=key_file,
                        allowed_hosts=(f"{host}:8788",), _env_file=None)


@pytest.mark.parametrize("host", ["10.0.0.2", "172.16.0.2", "192.168.134.128"])
def test_service_insecure_lan_supports_rfc1918_hosts(host):
    from autobuild_json.gateway.settings import ServiceSettings
    settings = ServiceSettings(host=host, allow_insecure_lan=True,
                               allowed_hosts=(f"{host}:8788",), _env_file=None)
    assert settings.host == host
    assert not settings.tls_proxy_configured
    assert settings.trusted_proxy_ips == ()


@pytest.mark.parametrize("allowed_hosts", [(), ("*",), ("*.example",), ("localhost:8788",),
                                         ("192.168.134.128:8788", "*")])
def test_service_insecure_lan_requires_exact_bind_host(allowed_hosts):
    from autobuild_json.gateway.settings import ServiceSettings
    with pytest.raises(ValueError):
        ServiceSettings(host="192.168.134.128", allow_insecure_lan=True,
                        allowed_hosts=allowed_hosts, _env_file=None)


def test_service_public_tls_proxy_configuration_still_supported():
    from autobuild_json.gateway.settings import ServiceSettings
    settings = ServiceSettings(host="0.0.0.0", tls_proxy_configured=True,
                               allowed_hosts=("gateway.example",),
                               trusted_proxy_ips=("127.0.0.1",), _env_file=None)
    assert settings.host == "0.0.0.0"


def test_maintenance_cli_closes_only_in_its_owned_event_loop(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace
    import autobuild_json.gateway.__main__ as cli
    from autobuild_json.gateway.settings import ServiceSettings
    services = SimpleNamespace(close_calls=0)
    async def close():
        services.close_calls += 1
    services.close = close
    settings = ServiceSettings(enabled=True, database_url="postgresql+psycopg://u:p@localhost/service",
                               master_key_file=tmp_path / "key", _env_file=None)
    monkeypatch.setattr("autobuild_json.gateway.settings.ServiceSettings", lambda **kwargs: settings)
    monkeypatch.setattr(cli, "build_services", lambda settings: services)
    async def run(services, stopped):
        assert asyncio.get_running_loop()
        await services.close()
    monkeypatch.setattr(cli, "run_maintenance", run)
    monkeypatch.setattr(sys, "argv", ["gateway", "maintenance"])
    assert cli.main() == 0
    assert services.close_calls == 1
