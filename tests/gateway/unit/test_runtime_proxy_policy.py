import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest


@pytest.mark.asyncio
async def test_operator_assigned_profile_authorizes_public_proxy_endpoint():
    from autobuild_json.gateway.transport.http import Transport, OutboundRequest
    from autobuild_json.gateway.transport.egress import EgressPolicy
    from autobuild_json.models import ProxyConfig
    async def resolve(host, port):
        return ["93.184.216.34"]
    transport = Transport(EgressPolicy(resolver=resolve), adapter=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True})))
    route = SimpleNamespace(root="https://provider.invalid/v1", proxy_profile_id=uuid4())
    async with transport.open(route, ProxyConfig("http://proxy.invalid:80"),
                              OutboundRequest("GET", "models", None, deadline=time.monotonic()+10)) as response:
        assert await response.read_json() == {"ok": True}
