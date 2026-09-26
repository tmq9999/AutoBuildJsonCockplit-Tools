from uuid import uuid4
from datetime import datetime, timedelta, timezone
import asyncio

import httpx
import pytest
from sqlalchemy import text

from tests.gateway.codex_admin_support import ORIGIN, account, private_env

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_every_mutation_is_private_session_origin_and_csrf_protected(pg_db, settings):
    from autobuild_json.gateway.http.app import create_gateway_app
    identity = uuid4()
    paths = [("PATCH", f"oauth-accounts/{identity}"), ("POST", f"oauth-accounts/{identity}/quota/refresh"),
             ("POST", f"oauth-accounts/{identity}/reset-credits/consume"), ("POST", f"reset-requests/{identity}/resolve"),
             ("PUT", "account-pools/vendor/model:v1"), ("POST", f"keys/{identity}/quota-adjust"),
             ("PUT", "codex-service/quota-refresh/settings"), ("POST", "codex-service/quota-refresh"),
             ("POST", "codex-service/quota-refresh/stop")]
    async with private_env(pg_db, settings, login=False) as (client, services):
        for method, path in paths:
            assert (await client.request(method, "/api/service/"+path, headers={"Origin":ORIGIN}, json={})).status_code == 401
        login = await client.post("/api/session", headers={"Origin":ORIGIN}, json={"token":"test-admin-token"})
        for method, path in paths:
            assert (await client.request(method, "/api/service/"+path, headers={"Origin":ORIGIN}, json={})).status_code == 403
            assert (await client.request(method, "/api/service/"+path,
                headers={"Origin":"http://evil.invalid", "X-CSRF-Token":login.json()["csrf_token"]}, json={})).status_code == 403
            assert (await client.request(method, "/api/service/"+path+"?limit=1",
                headers={"Origin":ORIGIN, "X-CSRF-Token":login.json()["csrf_token"]}, json={})).status_code == 400
        public = create_gateway_app(services.identity, services.catalog, services.engine, allowed_hosts=("public",))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="http://public") as other:
            for method, path in paths:
                assert (await other.request(method, "/api/service/"+path, json={})).status_code == 404
        async with pg_db.sessions() as session:
            for table in ("audit_events", "key_quota_adjustments", "codex_reset_requests", "requests"):
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_real_session_boundary_and_storage_only_safe_account_views(pg_db, settings):
    async with private_env(pg_db, settings, login=False) as (client, services):
        identity = await account(services)
        path = f"/api/service/oauth-accounts/{identity}"
        assert (await client.get("/api/service/oauth-accounts?limit=1")).status_code == 401
        login = await client.post("/api/session", headers={"Origin": ORIGIN}, json={"token": "test-admin-token"})
        client.headers["Origin"] = ORIGIN
        assert (await client.post(path + "/quota/refresh", json={})).status_code == 403
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        assert (await client.post(path + "/quota/refresh", headers={"Origin": "https://evil.invalid"}, json={})).status_code == 403
        response = await client.get("/api/service/oauth-accounts?limit=1")
        assert response.status_code == 200, response.text
        row = response.json()["items"][0]
        assert row["email"] == "synthetic@example.com"
        assert row["version"] == 1 and row["plan_type"] is None
        for secret in ("account-test", "synthetic-access", "synthetic-refresh", "encrypted_secret"):
            assert secret not in response.text
        assert "no-store" in response.headers["cache-control"].split(", ")
        quota = (await client.get(path + "/quota")).json()
        assert quota["snapshot"] is None and quota["stale"] is True and quota["last_error"] is None
        credits = (await client.get(path + "/reset-credits")).json()
        assert credits["snapshot"] is None and credits["active_reset"] is None and credits["stale"] is True
        for query in ("token=DO-NOT-ECHO", "limit=1&limit=2", "credential=DO-NOT-ECHO", "after=%00", "limit=201", "after=bad", "limit=0", "limit=1&cookie=x", "after=" + "a" * 17000):
            bad = await client.get("/api/service/oauth-accounts?" + query)
            assert bad.status_code in {400, 422} and "DO-NOT-ECHO" not in bad.text
        assert (await client.get(path + "/quota?limit=1")).status_code == 400
        assert (await client.get("/api/service/keys?limit=1")).status_code == 400
        from autobuild_json.gateway.http.app import create_gateway_app
        public = create_gateway_app(services.identity, services.catalog, services.engine, allowed_hosts=("public",))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="http://public") as other:
            for suffix in ("oauth-accounts", "capabilities", "usage/accounts", "usage/requests", "account-pools/m"):
                assert (await other.get("/api/service/" + suffix)).status_code == 404
        changed = await client.patch(path, json={"version": 1, "enabled": False, "proxy_profile_id": None})
        assert changed.status_code == 200 and changed.json()["version"] == 2
        assert (await client.patch(path, json={"version": 1, "enabled": True, "proxy_profile_id": None})).status_code == 409
        assert (await client.patch(path, json={"version": 2, "enabled": True, "proxy_profile_id": str(uuid4())})).status_code == 422
        assert (await client.patch(path, json={"version": 2, "enabled": True, "proxy_profile_id": None, "health": "active"})).status_code == 422


async def test_refresh_partial_and_reset_unknown_replay_resolution_are_safe(pg_db, settings):
    calls = []
    fail_credits = True
    def reply(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(500, text="SECRET-SENTINEL synthetic-refresh")
        if request.url.path.endswith("/usage"):
            return httpx.Response(200, json={"plan_type": "plus", "rate_limit": {"primary_window": {"used_percent": 25}}})
        if fail_credits:
            return httpx.Response(500, text="SECRET-SENTINEL synthetic-refresh")
        return httpx.Response(200, json={"available_count": 2, "credits": [{"id": "SECRET-CREDIT-ID", "state": "available"}]})
    async with private_env(pg_db, settings, reply) as (client, services):
        identity = await account(services)
        path = f"/api/service/oauth-accounts/{identity}"
        response = await client.post(path + "/quota/refresh", json={})
        assert response.status_code == 200, response.text
        assert response.json()["usage_status"] == "updated"
        assert response.json()["credits_status"] == "failed"
        assert response.json()["credits"]["last_error"] == "codex_credits_unavailable"
        assert "SECRET-SENTINEL" not in response.text
        assert (await client.post(path + "/quota/refresh", json={"force": True})).status_code == 422
        fail_credits = False
        response = await client.post(path + "/quota/refresh", json={})
        assert "SECRET-CREDIT-ID" not in response.text
        version = response.json()["credits"]["snapshot"]["version"]
        command = {"request_id": str(uuid4()), "credits_version": version, "acknowledge": True}
        for ack in (False, 1, "true"):
            assert (await client.post(path + "/reset-credits/consume", json=dict(command, acknowledge=ack))).status_code == 422
        result = await client.post(path + "/reset-credits/consume", json=command)
        assert result.status_code == 202 and result.json()["state"] == "unknown", result.text
        replay = await client.post(path + "/reset-credits/consume", json=command)
        assert replay.status_code == 202 and replay.json() == result.json()
        assert calls.count("POST") == 1
        assert (await client.post(path + "/reset-credits/consume", json=dict(command, credits_version=version+1))).status_code == 409
        assert (await client.patch(path, json={"version": 1, "enabled": False, "proxy_profile_id": None})).status_code == 409
        active = (await client.get(path + "/reset-credits")).json()["active_reset"]
        assert active == result.json()
        resolve = f"/api/service/reset-requests/{command['request_id']}/resolve"
        resolution = {"version": active["version"], "outcome": "confirmed_not_applied", "reason": "Provider evidence reviewed"}
        assert (await client.post(resolve, json=dict(resolution, version=1))).status_code == 409
        assert (await client.post(resolve, json=resolution)).json()["state"] == "rejected"
        terminal = await client.post(path + "/reset-credits/consume", json=command)
        assert terminal.status_code == 200 and terminal.json()["state"] == "rejected"
        assert calls.count("POST") == 1
        async with pg_db.sessions() as session:
            audit = str((await session.execute(text("SELECT details FROM audit_events"))).scalars().all())
        assert "SECRET-SENTINEL" not in audit and "SECRET-CREDIT-ID" not in audit


async def test_pool_create_only_cas_and_capabilities(pg_db, settings):
    from autobuild_json.gateway.routing.records import ModelConfig, BindingConfig
    async with private_env(pg_db, settings) as (client, services):
        identity = await account(services)
        async with pg_db.sessions() as session:
            provider = await session.scalar(text("SELECT provider_id FROM credentials WHERE id=:id"), {"id": identity})
        await services.catalog.put_model(ModelConfig(model_id="vendor/model:v1", identity="m", enabled=True))
        await services.catalog.put_binding(BindingConfig(uuid4(), provider, identity, "vendor/model:v1", "upstream", "m", 100, 100))
        path = "/api/service/account-pools/vendor/model:v1"
        empty = await client.get(path)
        assert empty.status_code == 200 and empty.json() == {"model_id": "vendor/model:v1", "version": 0, "policy": None}
        payload = {"version": 0, "policy": {"members": [{"credential_id": str(identity)}]}}
        created = await client.put(path, json=payload)
        assert created.status_code == 200 and created.json()["version"] == 1, created.text
        assert (await client.put(path, json=payload)).status_code == 409
        assert (await client.put(path, json=dict(payload, version=1))).json()["version"] == 2
        assert (await client.get(path)).json()["policy"]["members"][0]["credential_id"] == str(identity)
        matrix = (await client.get("/api/service/capabilities")).json()
        assert matrix["codex_oauth"]["responses"] is True and matrix["codex_oauth"]["compact"] is True
        assert set(matrix) == {"codex_oauth", "openai_compatible", "anthropic", "gemini", "ollama", "model_catalog"}
        assert any(model["id"] == "gpt-6-astra" for model in matrix["model_catalog"])


async def test_partial_refresh_retains_old_snapshot_but_marks_it_stale(pg_db, settings):
    usage_fails, credits_fail = False, False
    def reply(request):
        if request.url.path.endswith("/usage"):
            return httpx.Response(500, text="SECRET-ERROR") if usage_fails else httpx.Response(200, json={"plan_type":"plus"})
        return httpx.Response(500, text="SECRET-ERROR") if credits_fail else httpx.Response(200, json={"available_count":2})
    async with private_env(pg_db, settings, reply) as (client, services):
        identity = await account(services)
        path = f"/api/service/oauth-accounts/{identity}"
        first = (await client.post(path+"/quota/refresh", json={})).json()
        usage_fails = True
        result = await client.post(path+"/quota/refresh", json={})
        assert result.status_code == 200
        value = result.json()
        assert value["usage_status"] == "failed" and value["credits_status"] == "updated"
        assert value["usage"]["snapshot"]["version"] == first["usage"]["snapshot"]["version"]
        assert value["usage"]["stale"] is True and value["usage"]["last_error"] == "codex_usage_unavailable"
        credits_fail = True
        value = (await client.post(path+"/quota/refresh", json={})).json()
        assert value["credits_status"] == "failed" and value["credits"]["stale"] is True
        assert "SECRET-ERROR" not in str(value)
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE codex_account_state SET fetched_at=now()-interval '5 minutes',usage_error=NULL,credits_fetched_at=NULL,credits_error=NULL"))
        assert (await client.get(path+"/quota")).json()["stale"] is True
        assert (await client.get(path+"/reset-credits")).json()["stale"] is True
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM requests")) == 0


async def test_account_identity_proxy_audit_pagination_and_legacy_stamp_invalidation(pg_db, settings):
    from autobuild_json.gateway.admin.schemas import ProxyInput
    from autobuild_json.gateway.accounts.quota_records import CodexQuotaSnapshot, ResetCreditsSnapshot
    async with private_env(pg_db, settings) as (client, services):
        identities = [await account(services, str(n)) for n in range(3)]
        first = (await client.get("/api/service/oauth-accounts?limit=2")).json()
        second = (await client.get("/api/service/oauth-accounts", params={"limit":2, "after":first["next_after"]})).json()
        assert len(first["items"]) == 2 and len(second["items"]) == 1 and second["next_after"] is None
        assert len({r["id"] for r in first["items"]+second["items"]}) == 3
        for params in ({"credential_id":str(identities[0])}, {"model_id":"m"}, {"from":"2026-01-01"}):
            assert (await client.get("/api/service/oauth-accounts", params=params)).status_code == 400
        profile = await services.profiles.create(ProxyInput(name="Outbound reference", mode="direct"))
        identity = identities[0]
        path = f"/api/service/oauth-accounts/{identity}"
        changed = await client.patch(path, json={"version":1,"enabled":True,"proxy_profile_id":str(profile)})
        assert changed.status_code == 200
        row = next(r for r in (await client.get("/api/service/oauth-accounts")).json()["items"] if r["id"] == str(identity))
        assert row["proxy_profile_name"] == "Outbound reference" and row["proxy_profile_id"] == str(profile)
        async with pg_db.sessions() as session:
            audit = (await session.execute(text("SELECT actor,details FROM audit_events WHERE action='codex.account.updated'"))).mappings().one()
            assert audit["actor"] == "admin" and audit["details"]["version"] == 2
        now = datetime.now(timezone.utc)
        claim = await services.quota_store.begin_refresh(identity, now+timedelta(seconds=25))
        fetched = datetime.now(timezone.utc)
        await services.quota_store.save_refresh(claim, CodexQuotaSnapshot(credential_id=identity,version=1,fetched_at=fetched),
            ResetCreditsSnapshot(credential_id=identity,version=1,fetched_at=fetched,available_count=2), ())
        credits = await services.quota_store.read_credits(identity)
        reset = await services.quota_store.prepare_reset(identity, uuid4(), credits.version, "admin", deadline=datetime.now(timezone.utc)+timedelta(seconds=25))
        assert (await client.patch(path, json={"version":2,"enabled":False,"proxy_profile_id":None})).status_code == 409
        assert (await client.patch(f"/api/service/credentials/{identity}", json={"version":2,"enabled":False})).status_code == 200
        assert await services.quota_store.mark_dispatched(reset.result.operation_id, reset.generation) is False
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE credentials SET issuer='https://other.invalid' WHERE id=:id"), {"id":identities[1]})
        assert (await client.patch(f"/api/service/oauth-accounts/{identities[1]}", json={"version":1,"enabled":True,"proxy_profile_id":None})).status_code == 409


async def test_numbered_account_pages_filter_server_side_and_keep_cursor_compatibility(pg_db, settings):
    async with private_env(pg_db, settings) as (client, services):
        identities = [await account(services, str(n)) for n in range(5)]
        async with pg_db.sessions.begin() as session:
            for n, identity in enumerate(sorted(identities)):
                await session.execute(text("UPDATE credentials SET email=:email,enabled=:enabled WHERE id=:id"),
                                      {"email": f"USER{n}%_@example.invalid", "enabled": n != 4, "id": identity})
        numbered = await client.get('/api/service/oauth-accounts', params={'page': 2, 'limit': 2})
        assert numbered.status_code == 200
        body = numbered.json()
        assert body['page'] == 2 and body['page_size'] == 2 and body['total'] == 5
        assert body['total_pages'] == 3 and len(body['items']) == 2
        assert [r['id'] for r in body['items']] == list(map(str, sorted(identities)[2:4]))
        filtered = await client.get('/api/service/oauth-accounts', params={'page': 1, 'limit': 2, 'q': 'user4%_', 'status': 'disabled'})
        assert filtered.status_code == 200 and filtered.json()['total'] == 1
        assert filtered.json()['items'][0]['email'] == 'USER4%_@example.invalid'
        active = (await client.get('/api/service/oauth-accounts', params={'page': 1, 'limit': 2, 'status': 'active'})).json()
        assert active['total'] == 4 and all(r['enabled'] for r in active['items'])
        last = (await client.get('/api/service/oauth-accounts', params={'page': 999, 'limit': 2})).json()
        assert last['page'] == 3 and len(last['items']) == 1 and last['next_after'] is None
        empty = (await client.get('/api/service/oauth-accounts', params={'page': 999, 'limit': 2, 'q': "' OR 1=1 --"})).json()
        assert empty['page'] == 1 and empty['total'] == 0 and empty['total_pages'] == 1 and empty['items'] == []
        cursor = await client.get('/api/service/oauth-accounts', params={'limit': 2})
        assert cursor.status_code == 200 and cursor.json()['page'] is None and cursor.json()['next_after']
        assert (await client.get('/api/service/oauth-accounts', params={'page': 1, 'after': cursor.json()['next_after']})).status_code in {400, 422}
        for query in ('page=0', 'page=1000001', 'page=1&page=2', 'status=unknown', 'q='+'a'*321):
            assert (await client.get('/api/service/oauth-accounts?'+query)).status_code in {400, 422}


async def test_switching_proxy_reference_does_not_lock_two_profiles(pg_db, settings):
    from autobuild_json.gateway.admin.schemas import ProxyInput
    async with private_env(pg_db, settings) as (client, services):
        identity = await account(services)
        first = await services.profiles.create(ProxyInput(name="First", mode="direct"))
        second = await services.profiles.create(ProxyInput(name="Second", mode="direct"))
        path = f"/api/service/oauth-accounts/{identity}"
        assert (await client.patch(path, json={"version":1,"enabled":True,"proxy_profile_id":str(first)})).status_code == 200
        async with pg_db.sessions.begin() as session:
            await session.execute(text("SELECT id FROM proxy_profiles WHERE id=:id FOR UPDATE"), {"id":first})
            result = await asyncio.wait_for(client.patch(path,
                json={"version":2,"enabled":True,"proxy_profile_id":str(second)}), 1)
            assert result.status_code == 200


async def test_patch_reference_and_proxy_delete_serialize_before_reference_check(pg_db, settings, monkeypatch):
    from autobuild_json.gateway.admin.schemas import ProxyInput
    async with private_env(pg_db, settings) as (client, services):
        identity = await account(services)
        profile = await services.profiles.create(ProxyInput(name="Target", mode="direct"))
        entered, release = asyncio.Event(), asyncio.Event()
        real_audit = services.identity._audit
        async def audit_gate(*args, **kwargs):
            await real_audit(*args, **kwargs)
            entered.set()
            await release.wait()
        monkeypatch.setattr(services.identity, "_audit", audit_gate)
        patch = asyncio.create_task(client.patch(f"/api/service/oauth-accounts/{identity}",
            json={"version":1,"enabled":True,"proxy_profile_id":str(profile)}))
        await asyncio.wait_for(entered.wait(), 3)
        delete = asyncio.create_task(client.request("DELETE", f"/api/service/proxies/{profile}", json={"version":1}))
        async def blocked_or_finished():
            while not delete.done():
                async with pg_db.sessions() as session:
                    if await session.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid))>0 AND query LIKE '%proxy_profiles%')")):
                        return
                await asyncio.sleep(0.01)
        try:
            await asyncio.wait_for(blocked_or_finished(), 3)
        finally:
            release.set()
        patched, deleted = await asyncio.wait_for(asyncio.gather(patch, delete), 3)
        assert patched.status_code == 200 and deleted.status_code == 409
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT id FROM proxy_profiles WHERE id=:id"), {"id":profile}) == profile


@pytest.mark.parametrize("status,payload,code", [(401, {"error":"SECRET-KIOT-ERROR"}, "kiot_unavailable"),
    (200, {"success":False,"error":"KEY_NOT_FOUND", "message":"SECRET-KIOT-ERROR"}, "kiot_key_invalid"),
    (429, {"error":"SECRET-KIOT-ERROR"}, "proxy_not_ready")])
async def test_refresh_reports_proxy_error_without_upstream_secret_or_fake_success(pg_db, settings, status, payload, code):
    from autobuild_json.gateway.admin.schemas import ProxyInput
    from autobuild_json.gateway.proxy.kiot import KiotClient
    from pydantic import SecretStr
    async with private_env(pg_db, settings) as (client, services):
        identity = await account(services)
        profile = await services.profiles.create(ProxyInput(name="Pool",mode="kiotproxy", entries_text=SecretStr("fixture-kiot-key")))
        services.proxies.kiot = KiotClient(adapter=httpx.MockTransport(
            lambda request: httpx.Response(status, json=payload)))
        path = f"/api/service/oauth-accounts/{identity}"
        assert (await client.patch(path, json={"version":1,"enabled":True,"proxy_profile_id":str(profile)})).status_code == 200
        response = await client.post(path+"/quota/refresh", json={})
        assert response.status_code >= 400
        assert response.json()["error"]["code"] == code
        assert "SECRET-KIOT-ERROR" not in response.text and "fixture-kiot-key" not in response.text
        assert (await client.get(path+"/quota")).json()["snapshot"] is None
