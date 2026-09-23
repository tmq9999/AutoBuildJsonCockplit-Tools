from pathlib import Path
import subprocess
import sys

import pytest

from scripts.gateway_testdb import validate_test_url


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://u:p@10.0.0.1:5432/abgw_test_" + "a" * 32,
    "postgresql+psycopg://u:p@localhost:5432/production",
    "postgresql+psycopg://u:p@localhost:5432/abgw_test_" + "a" * 32 + "?host=evil.invalid",
])
def test_testdb_rejects_external_or_ambiguous_targets(url):
    with pytest.raises(ValueError, match="dedicated"):
        validate_test_url(url)


def test_testdb_cli_requires_explicit_acknowledgment(tmp_path):
    script = Path(__file__).resolve().parents[3] / "scripts/gateway_testdb.py"
    result = subprocess.run([sys.executable, str(script), "--postgres-bin", str(tmp_path)],
                            text=True, capture_output=True)
    assert result.returncode == 2
    assert "acknowledge-test-only" in result.stderr
    assert not list(tmp_path.iterdir())


def test_network_guard_blocks_unrelated_ports():
    import socket
    with socket.socket() as connection:
        with pytest.raises(AssertionError, match="Outbound network"):
            connection.connect(("127.0.0.1", 11111))
