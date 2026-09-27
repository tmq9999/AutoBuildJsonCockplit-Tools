import importlib.util
from pathlib import Path


def _harness_module():
    path = Path(__file__).parents[3] / "data" / "bounded_acceptance_verify.py"
    spec = importlib.util.spec_from_file_location("bounded_acceptance_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upstream_status_delta_reads_redacted_capacity_snapshots():
    harness = _harness_module()
    before = {"upstream": {"http_statuses": {"429": 2, "502": 1}}}
    after = {"upstream": {"http_statuses": {"429": 33, "502": 3, "503": 4}}}

    assert harness.upstream_status_delta(before, after) == {"429": 31, "502": 2, "503": 4}
