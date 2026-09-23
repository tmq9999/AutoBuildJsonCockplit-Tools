from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import httpx

from tests.gateway.integration.test_catalog import catalog_case


def chat_success(usage=True):
    chunks = [{"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}]}]
    if usage:
        chunks.append({"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 5}})
    return b"".join(("data: "+json.dumps(chunk)+"\n\n").encode() for chunk in chunks)+b"data: [DONE]\n\n"


@asynccontextmanager
async def gateway_environment(db, *, respond=None, quota=1_000_000_000_000):
    from autobuild_json.gateway.identity.service import IdentityService
    from autobuild_json.gateway.identity.policy import KeyPolicy
    from autobuild_json.gateway.metering.ledger import Ledger
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.pg_leases import PgLeaseStore
    from autobuild_json.gateway.transport.http import Transport
    from autobuild_json.gateway.transport.egress import EgressPolicy
    from autobuild_json.gateway.engine import Engine
    from autobuild_json.gateway.http.app import create_gateway_app
    catalog, principal, provider, credential, binding = await catalog_case(db)
    identity = IdentityService(db, b"p"*32)
    issued = await identity.create_key(principal.customer_id, KeyPolicy(model_ids={"public"}, protocols={"openai"},
                                                                       total_micro=quota, concurrency=10))
    sent = []
    def handle(request):
        sent.append({"url": str(request.url), "headers": dict(request.headers), "body": json.loads(request.content)})
        return respond(request) if respond else httpx.Response(200, content=chat_success(), headers={"content-type": "text/event-stream"})
    async def resolver(host, port):
        return ["93.184.216.34"]
    transport = Transport(EgressPolicy(resolver=resolver), adapter=httpx.MockTransport(handle))
    ledger = Ledger(db)
    engine = Engine(db, catalog, ledger, ProxyManager(PgLeaseStore(db)), transport, catalog.vault)
    app = create_gateway_app(identity, catalog, engine, allowed_hosts={"127.0.0.1:8788"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8788") as client:
        yield SimpleNamespace(app=app, client=client, secret=issued.secret, key_id=issued.key_id, ledger=ledger, engine=engine,
                              upstream_requests=sent, provider_id=provider, credential_id=credential, binding=binding)
