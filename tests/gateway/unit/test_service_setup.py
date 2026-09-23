import subprocess
import sys

import pytest
from pydantic import ValidationError


def test_disabled_service_needs_no_database_or_keys():
    from autobuild_json.gateway.settings import ServiceSettings
    settings = ServiceSettings(_env_file=None)
    assert settings.enabled is False
    assert settings.host == "127.0.0.1"
    assert settings.port == 8788


def test_enabled_service_requires_database_and_key_file():
    from autobuild_json.gateway.settings import ServiceSettings
    with pytest.raises(ValidationError):
        ServiceSettings(enabled=True, _env_file=None)


@pytest.mark.parametrize("url", ["sqlite:///test.db", "postgresql://u:p@localhost/prod", "http://example.com"])
def test_service_rejects_wrong_database_driver(url, tmp_path):
    from autobuild_json.gateway.settings import ServiceSettings
    with pytest.raises(ValidationError):
        ServiceSettings(enabled=True, database_url=url, master_key_file=tmp_path / "key", _env_file=None)


def test_service_database_secret_is_hidden(tmp_path):
    from autobuild_json.gateway.settings import ServiceSettings
    settings = ServiceSettings(enabled=True, database_url="postgresql+psycopg://u:secret@localhost/service",
                               master_key_file=tmp_path / "key", _env_file=None)
    assert "secret@" not in repr(settings)


def test_legacy_imports_without_service_dependencies():
    result = subprocess.run([sys.executable, "-c", """
import sys
class RejectServiceImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'sqlalchemy', 'psycopg', 'alembic'}:
            raise ImportError('optional dependency accessed')
sys.meta_path.insert(0, RejectServiceImports())
import autobuild_json.gateway
import autobuild_json.api
print('legacy-ok')
"""], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "legacy-ok"
