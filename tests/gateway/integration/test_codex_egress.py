"""Codex HTTP ingress reaches configured egress, including OAuth refresh.

Only socket/curl boundaries are synthetic: real proxy selection, leases,
httpcore CONNECT, OAuth refresh/JWT checks and customer accounting run here.
"""
import json
import time

import httpcore
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from curl_cffi import CurlOpt
from pydantic import SecretStr
from sqlalchemy import text

from autobuild_json.gateway.accounts.imports import CODEX_PROVIDER
from autobuild_json.gateway.admin.schemas import ProxyInput
from autobuild_json.gateway.proxy.kiot import KiotClient
from autobuild_json.gateway.proxy.profiles import ProfileStore
from autobuild_json.gateway.routing.records import ProviderConfig
from tests.fakes import Response
from .test_codex_pool_dispatch import BODY, codex_environment
from .test_responses_gateway import native_wire

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


class SyntheticSocket(httpcore.AsyncNetworkStream):
    def __init__(self, owner, port):
        self.owner, self.port = owner, port
        self.writes, self.tls_names = [], []
        self.connect_returned = port == 443
        self.replied, self.closed = False, False

    async def read(self, max_bytes, timeout=None):
        if not self.connect_returned:
            self.connect_returned = True
            return b"HTTP/1.1 200 Connection established\r\n\r\n"
        if self.replied:
            return b""
        self.replied = True
        body = native_wire()
        return (b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
                + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)

    async def write(self, buffer, timeout=None):
        self.writes.append(bytes(buffer))

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls_names.append(server_hostname)
        return self

    def get_extra_info(self, info):
        return None

    async def aclose(self):
        self.closed = True


class SyntheticNetwork(httpcore.AsyncNetworkBackend):
    def __init__(self, *, fail=False):
        self.connections, self.sockets, self.fail = [], [], fail

    async def connect_tcp(self, host, port, **kwargs):
        self.connections.append((host, port))
        if self.fail:
            raise httpcore.ConnectError("DO-NOT-ECHO-proxy-connect")
        stream = SyntheticSocket(self, port)
        self.sockets.append(stream)
        return stream

    async def connect_unix_socket(self, *args, **kwargs):
        raise AssertionError("No Unix socket expected")


def install_network(monkeypatch, *, fail=False):
    from autobuild_json.gateway.transport import network

    backend = SyntheticNetwork(fail=fail)
    monkeypatch.setattr(network, "AutoBackend", lambda: backend)
    return backend


def install_oauth_http(monkeypatch):
    from autobuild_json import transport as legacy_transport

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="egress-fixture", alg="RS256", use="sig")
    claims = {"iss": "https://auth.openai.com", "aud": "app_EMoamEEZ73f0CkXaXp7hrann",
        "iat": int(time.time()), "exp": int(time.time()) + 3600, "sub": "fixture",
        "email": "synthetic@example.com", "email_verified": True,
        "https://api.openai.com/auth": {"chatgpt_account_id": "account-0"}}
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "egress-fixture"})
    calls, closed = [], []

    class RawSession:
        def __init__(self, **kwargs):
            self.cookies, self.curl_options = {}, {}

        def request(self, **kwargs):
            calls.append((kwargs["method"], kwargs["url"], self.curl_options.get(CurlOpt.PROXY)))
            assert kwargs["allow_redirects"] is False
            if kwargs["method"] == "POST":
                assert kwargs["data"]["grant_type"] == "refresh_token"
                payload = {"id_token": token, "access_token": "fresh-egress-access",
                           "refresh_token": "fresh-egress-refresh", "expires_in": 3600}
            else:
                payload = {"keys": [jwk]}
            kwargs["content_callback"](json.dumps(payload).encode())
            return Response(payload)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(legacy_transport.requests, "Session", RawSession)
    return calls, closed


async def configure_egress(db, env, mode, *, override=None, kiot_failure=False):
    profiles = ProfileStore(db, env.engine.vault, b"p" * 32)
    env.engine.proxy_resolver = profiles.load
    env.engine.credentials.profile_resolver = profiles.load
    env.engine.credentials.proxies = env.engine.proxies
    # Remove the harness MockTransport so real httpcore's proxy factory runs.
    env.engine.transport.adapter = None
    profile_ids = []

    async def profile(kind, prefix):
        entries = {"direct": "", "fixed": f"http://{prefix}-fixed.invalid:8101",
            "pool": f"http://{prefix}-pool.invalid:8102\nhttp://{prefix}-pool-next.invalid:8103",
            "kiotproxy": f"DO-NOT-ECHO-{prefix}-KIOT"}[kind]
        identity = await profiles.create(ProxyInput(name=f"{prefix} {kind}", mode=kind,
                                                    entries_text=SecretStr(entries)))
        profile_ids.append(identity)
        return identity

    default = await profile(mode, "default")
    async with db.sessions() as session:
        row = (await session.execute(text("SELECT config,version FROM providers WHERE id=:id"),
                                     {"id": CODEX_PROVIDER})).mappings().one()
    config = ProviderConfig.model_validate(row["config"]).model_copy(update={"proxy_profile_id": default})
    await env.engine.catalog.update_provider(CODEX_PROVIDER, row["version"], config)
    if override is not None:
        chosen = await profile(override, "override")
        async with db.sessions.begin() as session:
            await session.execute(text("UPDATE credentials SET profile_id=:profile,version=version+1 WHERE id=:id"),
                                  {"profile": chosen, "id": env.bindings[0].credential_id})
    kiot_calls = []

    def kiot_reply(request):
        kiot_calls.append(request)
        if kiot_failure:
            return httpx.Response(200, json={"success": False, "error": "KEY_NOT_FOUND",
                                            "message": "DO-NOT-ECHO-KIOT-failure"})
        now = int(time.time() * 1000)
        return httpx.Response(200, json={"success": True, "data": {"http": "93.184.216.34:8104",
            "ttl": 600, "ttc": 0, "expirationAt": now + 600000, "nextRequestAt": now}, "timestamp": now})

    env.engine.proxies.kiot = KiotClient(adapter=httpx.MockTransport(kiot_reply))
    return kiot_calls, profile_ids


async def assert_no_leases(db):
    async with db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM proxy_leases WHERE owner IS NOT NULL")) == 0
        assert await session.scalar(text("SELECT count(*) FROM provider_admissions WHERE active")) == 0


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("mode,override,want_refresh,want_port", [
    ("fixed", None, "http://default-fixed.invalid:8101", 8101),
    ("pool", None, "http://default-pool.invalid:8102", 8103),
    ("kiotproxy", None, "http://93.184.216.34:8104", 8104),
    ("kiotproxy", "fixed", "http://override-fixed.invalid:8101", 8101),
    ("fixed", "pool", "http://override-pool.invalid:8102", 8103),
    ("fixed", "kiotproxy", "http://93.184.216.34:8104", 8104),
    ("kiotproxy", "direct", "", 443),
])
async def test_refresh_and_json_sse_inference_use_effective_egress(
        pg_db, monkeypatch, stream, mode, override, want_refresh, want_port):
    backend = install_network(monkeypatch)
    refresh_calls, refresh_closed = install_oauth_http(monkeypatch)
    async with codex_environment(pg_db) as env:
        kiot_calls, profile_ids = await configure_egress(pg_db, env, mode, override=override)
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE credentials SET token_expires_at=now()-interval '1 second' WHERE id=:id"),
                                  {"id": env.bindings[0].credential_id})
        result = await env.client.post("/v1/responses", json=dict(BODY, stream=stream), headers=env.headers)
        assert result.status_code == 200, result.text
        if stream:
            assert result.headers["content-type"].startswith("text/event-stream")
            assert "event: response.completed" in result.text and '"Native"' in result.text
        else:
            assert result.json()["status"] == "completed"
            assert result.json()["output"][0]["content"][0]["text"] == "Native"
        # GuardedBackend supplies an approved literal to the socket. Direct
        # override reaches 443; proxies reach only their configured ports.
        assert backend.connections == [("93.184.216.34", want_port)]
        socket = backend.sockets[0]
        wire = b"".join(socket.writes)
        assert (b"CONNECT chatgpt.com:443 HTTP/1.1" in wire) == (want_port != 443)
        assert b"POST /backend-api/codex/responses HTTP/1.1" in wire
        assert b"authorization: bearer fresh-egress-access" in wire.lower()
        assert b"chatgpt-account-id: account-0" in wire
        assert socket.tls_names == ["chatgpt.com"] and socket.closed
        assert len(refresh_calls) == len(refresh_closed) == 2
        assert refresh_calls[0][:2] == ("POST", "https://auth.openai.com/oauth/token")
        assert [r[0] for r in refresh_calls] == ["POST", "GET"]
        assert [r[2] for r in refresh_calls] == [want_refresh, want_refresh]
        effective = override or mode
        assert len(kiot_calls) == (2 if effective == "kiotproxy" else 0)
        if kiot_calls:
            assert all(r.url.path == "/api/v1/proxies/current" for r in kiot_calls)
        assert not any(name.lower().startswith("x-codex-proxy") for name in result.headers)
        for private in ["DO-NOT-ECHO", "fresh-egress", "proxy_profile_id", "default-fixed.invalid",
                        "override-fixed.invalid", *(str(identity) for identity in profile_ids)]:
            assert private not in result.text
        async with pg_db.sessions() as session:
            assert (await session.execute(text("SELECT state FROM requests"))).scalar_one() == "completed"
            assert (await session.execute(text("SELECT count(*),sum(amount_micro) FROM usage_ledger"))).one() == (1, 23)
            assert (await session.execute(text("SELECT spent,held FROM quota_buckets WHERE window_kind='total'"))).one() == (23, 0)
        await assert_no_leases(pg_db)


@pytest.mark.parametrize("mode", ["fixed", "pool", "kiotproxy"])
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
async def test_configured_proxy_connection_failure_never_falls_back_direct(pg_db, monkeypatch, mode, stream):
    backend = install_network(monkeypatch, fail=True)
    async with codex_environment(pg_db) as env:
        _, profile_ids = await configure_egress(pg_db, env, mode)
        result = await env.client.post("/v1/responses", json=dict(BODY, stream=stream), headers=env.headers)
        assert result.status_code == 502, result.text
        assert result.json()["error"]["code"] == "proxy_error"
        # Task 1 permits exactly one retry for a configured-proxy failure
        # proven before response headers. Both attempts must still use a
        # configured proxy; direct egress is never a fallback.
        assert len(backend.connections) == 2
        assert all(connection[1] in {8101, 8102, 8103, 8104} for connection in backend.connections)
        assert not any(name.lower().startswith("x-codex-proxy") for name in result.headers)
        assert "DO-NOT-ECHO" not in result.text
        assert all(str(identity) not in result.text for identity in profile_ids)
        async with pg_db.sessions() as session:
            # A failed call after the dispatch boundary is conservatively
            # pending, never falsely billed/refunded as a completed response.
            assert await session.scalar(text("SELECT state FROM requests")) == "usage_pending"
            assert await session.scalar(text("SELECT count(*) FROM attempts")) == 2
            assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 0
        await assert_no_leases(pg_db)


async def test_kiot_allocation_failure_has_no_inference_socket_or_direct_fallback(pg_db, monkeypatch):
    backend = install_network(monkeypatch)
    async with codex_environment(pg_db) as env:
        calls, _ = await configure_egress(pg_db, env, "kiotproxy", kiot_failure=True)
        result = await env.client.post("/v1/responses", json=BODY, headers=env.headers)
        assert result.status_code == 400 and result.json()["error"]["code"] == "kiot_key_invalid", result.text
        assert len(calls) == 1 and not backend.connections
        assert "DO-NOT-ECHO" not in result.text
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT state FROM requests")) == "released"
            assert await session.scalar(text("SELECT count(*) FROM attempts")) == 0
            assert await session.scalar(text("SELECT held FROM quota_buckets WHERE window_kind='total'")) == 0
        await assert_no_leases(pg_db)
