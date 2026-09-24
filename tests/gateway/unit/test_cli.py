import subprocess
import sys


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
