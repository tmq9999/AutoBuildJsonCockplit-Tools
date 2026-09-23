import threading
import time
import pytest
from autobuild_json.settings import Settings
from autobuild_json.models import AttemptContext

@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, installation_id="00000000-0000-4000-8000-000000000001", admin_token="test-admin-token", _env_file=None)

@pytest.fixture
def context():
    return AttemptContext(time.monotonic() + 180, threading.Event(), lambda *args: None)
