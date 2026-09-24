"""Real private app/PostgreSQL with synthetic provider I/O for Chrome tests."""

import argparse
import asyncio
import os
from pathlib import Path
import socket
import signal
from contextlib import contextmanager

import httpx
import uvicorn
from pydantic import SecretStr

from autobuild_json.api import create_app
from autobuild_json.settings import Settings
from autobuild_json.results import RunStore
from autobuild_json.runner import Runner
from autobuild_json.gateway.admin.services import AdminServices
from autobuild_json.gateway.secrets import Vault
from autobuild_json.gateway.storage.db import make_database
from autobuild_json.gateway.storage.migrate import upgrade
from autobuild_json.gateway.transport.egress import EgressPolicy
from autobuild_json.gateway.transport.http import Transport
from scripts.gateway_testdb import test_database, validate_test_url
from tests.fakes import success_result
from tests.gateway.harness import chat_success


class TestServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        previous = signal.signal(signal.SIGTERM, lambda *_: setattr(self, 'should_exit', True))
        try:
            yield
        finally:
            signal.signal(signal.SIGTERM, previous)


def database_options(environment, root):
    url = environment.get("AUTOBUILD_TEST_DATABASE_URL")
    if "AUTOBUILD_TEST_DATABASE_URL" in environment:
        validate_test_url(url or "")
    return {
        "postgres_bin": environment.get("AUTOBUILD_TEST_POSTGRES_BIN") or root / ".deps/gateway-pg17/binary/bin",
        "url": url,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with test_database(**database_options(os.environ, root)) as url:
        db = make_database(SecretStr(url))
        asyncio.run(upgrade(db))

        async def resolver(host, port):
            return ["93.184.216.34"]

        def respond(request):
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "test-model"}]})
            return httpx.Response(200, content=chat_success())

        services = AdminServices(
            db,
            Vault({"v1": b"a" * 32, "client_keys": b"p" * 32}, active="v1"),
            b"p" * 32,
            transport=Transport(EgressPolicy(resolver=resolver), adapter=httpx.MockTransport(respond)),
        )
        settings = Settings(
            port=args.port,
            data_dir=Path(os.environ["AUTOBUILD_TEST_DATA"]),
            admin_token="browser-test-admin",
            _env_file=None,
        )
        runner = Runner(
            RunStore(settings.data_dir / "runs"),
            lambda account, proxy, context: success_result(account.email),
        )

        def denied(*args, **kwargs):
            raise AssertionError("Provider network forbidden in browser test")

        socket.socket.connect = denied
        app = create_app(settings, runner=runner, gateway_services=services)
        TestServer(uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")).run()


if __name__ == "__main__":
    main()
