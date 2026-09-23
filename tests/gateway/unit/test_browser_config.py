from pathlib import Path

import pytest


def test_browser_server_honors_external_test_binary_path():
    from tests.gateway_ui_server import database_options
    options = database_options({"AUTOBUILD_TEST_POSTGRES_BIN": "/opt/postgres/bin"}, Path("/checkout"))
    assert options == {"postgres_bin": "/opt/postgres/bin", "url": None}


def test_browser_server_accepts_same_dedicated_test_url_as_pytest():
    from tests.gateway_ui_server import database_options
    url = "postgresql+psycopg://u:synthetic@127.0.0.1:55432/abgw_test_" + "a" * 32
    options = database_options({"AUTOBUILD_TEST_DATABASE_URL": url}, Path("/checkout"))
    assert options["url"] == url
    assert options["postgres_bin"] == Path("/checkout/.deps/gateway-pg17/binary/bin")


def test_browser_server_rejects_invalid_explicit_database_instead_of_fallback():
    from tests.gateway_ui_server import database_options
    for value in ("", "postgresql+psycopg://u:synthetic@localhost:5432/production"):
        with pytest.raises(ValueError, match="dedicated"):
            database_options({"AUTOBUILD_TEST_DATABASE_URL": value}, Path("/checkout"))
