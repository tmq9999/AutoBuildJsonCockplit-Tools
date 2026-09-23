"""Browser-test server only. Never imports/executes real account login."""
import argparse
import os
import socket
from pathlib import Path

import uvicorn
from curl_cffi import requests

from autobuild_json.api import create_app
from autobuild_json.errors import FlowError
from autobuild_json.models import RunResult
from autobuild_json.results import RunStore
from autobuild_json.runner import Runner
from autobuild_json.settings import Settings
from tests.fakes import success_result


def deny_network(*args, **kwargs):
    raise AssertionError("Provider network forbidden in browser tests")


def processor(account, proxy, context):
    context.on_stage("email", 1)
    context.cancel.wait(20 if account.email.startswith("slow") else .15)
    context.check()
    if account.email.startswith("phone"):
        return RunResult("phone_verify", error=FlowError("PHONE_VERIFY"))
    if account.email.startswith("error"):
        return RunResult("error", error=FlowError("INVALID_CREDENTIALS"))
    return success_result(account.email)


class FakeTokens:
    def complete(self, grant, transport):
        return success_result(grant.expected_email).record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    settings = Settings(port=args.port, data_dir=Path(os.environ["AUTOBUILD_TEST_DATA"]),
                        admin_token="browser-test-admin", _env_file=None)
    requests.Session.request = deny_network
    socket.socket.connect = deny_network
    runner = Runner(RunStore(settings.data_dir / "runs"), processor)
    app = create_app(settings, runner=runner, token_service=FakeTokens())
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
