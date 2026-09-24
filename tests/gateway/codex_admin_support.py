from contextlib import asynccontextmanager

import httpx
from sqlalchemy import text

from autobuild_json.api import create_app
from autobuild_json.gateway.admin.services import AdminServices
from autobuild_json.gateway.transport.egress import EgressPolicy
from autobuild_json.gateway.transport.http import Transport
from autobuild_json.results import RunStore
from autobuild_json.runner import Runner
from tests.gateway.catalog_support import synthetic_vault
from tests.gateway.integration.test_oauth_import import record

ORIGIN = "http://127.0.0.1:8787"


@asynccontextmanager
async def private_env(db, settings, responder=None, *, login=True):
    async def resolve(host, port):
        return ["93.184.216.34"]

    def default_reply(request):
        raise AssertionError("Unexpected account HTTP")

    transport = Transport(EgressPolicy(resolver=resolve), adapter=httpx.MockTransport(responder or default_reply))
    services = AdminServices(db, synthetic_vault(), b"p" * 32, transport=transport)
    runner = Runner(RunStore(settings.data_dir / "runs"), lambda *args: None)
    app = create_app(settings, runner=runner, gateway_services=services)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            if login:
                response = await client.post("/api/session", headers={"Origin": ORIGIN}, json={"token": "test-admin-token"})
                assert response.status_code == 200
                client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]})
            yield client, services


async def account(services, suffix=""):
    value = record()
    value["account"]["id"] += suffix
    identity = await services.engine.credentials.import_record(value, None)
    async with services.db.sessions.begin() as session:
        await session.execute(text("UPDATE credentials SET health='active',token_expires_at=now()+interval '1 hour' WHERE id=:id"), {"id": identity})
    return identity
