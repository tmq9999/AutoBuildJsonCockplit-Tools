from pathlib import Path
import pytest
from autobuild_json.checklive_adapter import load_checklive
from autobuild_json.errors import FlowError

def test_missing_dependency_fails_safe(tmp_path):
    with pytest.raises(FlowError) as err:
        load_checklive(tmp_path)
    assert err.value.code == "CONFIGURATION_ERROR"
    assert str(tmp_path) not in str(err.value)

def test_pinned_dependency_loads_without_solver():
    module = load_checklive(Path(".deps/Check-Account-ChatGPT"))
    assert callable(module.AuthSession.verify_password)
    assert not module.sentinel.HAS_SENTINEL_VM
    assert module.config.DEBUG is False
