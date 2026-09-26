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
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import json
from sqlalchemy import text


async def seed_codex(services):
    """Synthetic records only; exercise real stores, ledger and admin routes."""
    from autobuild_json.gateway.routing.records import ModelConfig, BindingConfig
    from autobuild_json.gateway.accounts.imports import CODEX_PROVIDER
    from autobuild_json.gateway.identity.policy import KeyPolicy
    from autobuild_json.gateway.metering.records import Admission, Bounds, Usage
    from autobuild_json.gateway.admin.schemas import ProxyInput
    accounts = []
    for name in ('alpha', 'beta', 'charlie'):
        identity = await services.engine.credentials.import_record({
            'id': str(uuid4()), 'email': name+'@example.invalid', 'account': {'id': 'SYNTHETIC-ACCOUNT-'+name},
            'tokens': {'id_token': 'SYNTHETIC-ID', 'access_token': 'SYNTHETIC-ACCESS', 'refresh_token': 'SYNTHETIC-REFRESH'}}, None)
        accounts.append(identity)
    async with services.db.sessions.begin() as session:
        await session.execute(text("UPDATE credentials SET health='active',token_expires_at=now()+interval '1 hour'"))
    model = 'vendor/model:v1'
    await services.catalog.put_model(ModelConfig(model_id=model, identity='synthetic-model', enabled=True,
        input_micro=1000000, output_micro=3000000, cache_read_micro=100000, cache_write_micro=1250000))
    for identity in accounts:
        await services.catalog.put_binding(BindingConfig(uuid4(), CODEX_PROVIDER, identity, model, 'upstream', 'synthetic-model', 4096, 1024))
    for mode in ('direct', 'fixed', 'pool', 'kiotproxy'):
        entries = '' if mode == 'direct' else 'synthetic-kiot' if mode == 'kiotproxy' else 'http://proxy.invalid:8080'
        await services.profiles.create(ProxyInput(name=mode+' demo', mode=mode, entries_text=SecretStr(entries)))
    owner = await services.identity.create_customer('Synthetic customer')
    issued = await services.identity.create_key(owner, KeyPolicy(model_ids={model}, protocols={'openai'}, total_micro=100000000000000), 'Demo key')
    principal = await services.identity.authenticate(issued.secret, 'openai')
    admission = Admission(uuid4(), principal, model, Bounds(1000, 200), 1000000, 3000000,
        datetime.now(timezone.utc)+timedelta(seconds=60), cache_read_micro=100000, cache_write_micro=1250000)
    await services.ledger.reserve(admission)
    rejected, completed = uuid4(), uuid4()
    await services.ledger.mark_dispatched(admission.request_id, rejected)
    await services.ledger.mark_rejected(admission.request_id, rejected, 'rejected_before_generation')
    await services.ledger.mark_dispatched(admission.request_id, completed)
    await services.ledger.settle(admission.request_id, Usage(1000, 200, cached_read=600, cached_write=100))
    async with services.db.sessions.begin() as session:
        await session.execute(text('UPDATE attempts SET credential_id=:id WHERE id=:attempt'), {'id':accounts[1], 'attempt':rejected})
        await session.execute(text("UPDATE attempts SET credential_id=:id,status='completed',usage=CAST(:usage AS jsonb) WHERE id=:attempt"),
            {'id':accounts[0], 'attempt':completed, 'usage':json.dumps({'input_tokens':1000,'output_tokens':200,'cached_read':600,'cached_write':100,'reasoning':0})})


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
    parser.add_argument("--codex", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with test_database(**database_options(os.environ, root)) as url:
        db = make_database(SecretStr(url))
        asyncio.run(upgrade(db))

        async def resolver(host, port):
            return ["93.184.216.34"]

        reset_count, fail_reads = 0, 0

        def respond(request):
            nonlocal reset_count, fail_reads
            if '/wham/' in request.url.path:
                if request.method == 'POST':
                    reset_count += 1
                    assert reset_count == 1, 'A reset was sent twice upstream'
                    fail_reads = 2
                    return httpx.Response(200)
                if fail_reads:
                    fail_reads -= 1
                    return httpx.Response(500, text='SYNTHETIC-SECRET-ERROR')
                if request.url.path.endswith('/usage'):
                    return httpx.Response(200, json={'plan_type':'plan-synthetic','rate_limit':{'allowed':True,
                        'primary_window':{'used_percent':42, 'limit_window_seconds':18000, 'reset_after_seconds':3600},
                        'secondary_window':{'used_percent':18, 'limit_window_seconds':604800}},
                        'additional_rate_limits':[{'limit_name':'gpt-reserve','rate_limit':{'allowed':True,
                            'primary_window':{'used_percent':25,'limit_window_seconds':18000,'reset_after_seconds':7200}}}]})
                return httpx.Response(200, json={'available_count':2-reset_count,'credits':[
                    {'id':'SYNTHETIC-CREDIT-ID','state':'available'}]})
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "test-model"}]})
            return httpx.Response(200, content=chat_success())

        services = AdminServices(
            db,
            Vault({"v1": b"a" * 32, "client_keys": b"p" * 32}, active="v1"),
            b"p" * 32,
            transport=Transport(EgressPolicy(resolver=resolver), adapter=httpx.MockTransport(respond)),
        )
        if args.codex:
            asyncio.run(seed_codex(services))
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
