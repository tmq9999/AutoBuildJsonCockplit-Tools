from contextlib import asynccontextmanager

import httpx
import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@asynccontextmanager
async def admin_env(pg_db):
    from fastapi import FastAPI, Request, HTTPException
    from autobuild_json.gateway.admin.routes import create_admin_router
    from autobuild_json.gateway.admin.services import AdminServices
    from autobuild_json.gateway.secrets import Vault
    services = AdminServices(pg_db, Vault({"v1": b"a"*32}, active="v1"), b"p"*32)
    app = FastAPI()
    def authorized(request: Request):
        if request.headers.get("x-test-session") != "synthetic-session":
            raise HTTPException(401)
        if request.method != "GET" and request.headers.get("x-csrf-token") != "synthetic-csrf":
            raise HTTPException(403)
    app.include_router(create_admin_router(services, authorized))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://admin") as client:
        yield client, services


async def test_admin_key_crud_is_private_and_secret_shown_once(pg_db):
    async with admin_env(pg_db) as (client, services):
        assert (await client.get("/api/service/customers")).status_code == 401
        client.headers.update({"x-test-session": "synthetic-session", "x-csrf-token": "synthetic-csrf"})
        customer = (await client.post("/api/service/customers", json={"name": "Test customer"})).json()["id"]
        created = await client.post("/api/service/keys", json={"customer_id": customer, "policy": {
            "model_ids": ["public"], "protocols": ["openai"], "total_micro": 1_000_000}})
        assert created.status_code == 201, created.text
        secret, identity = created.json()["secret"], created.json()["key_id"]
        listing = await client.get("/api/service/keys")
        assert secret not in listing.text and "secret_digest" not in listing.text
        rotated = await client.post(f"/api/service/keys/{identity}/rotate", json={"version": 1})
        assert rotated.json()["secret"] != secret
        revoked = await client.post(f"/api/service/keys/{identity}/revoke", json={"version": 2})
        assert revoked.status_code == 200


async def test_provider_and_proxy_secrets_are_write_only(pg_db):
    async with admin_env(pg_db) as (client, services):
        client.headers.update({"x-test-session": "synthetic-session", "x-csrf-token": "synthetic-csrf"})
        provider = await client.post("/api/service/providers", json={"name": "Upstream", "adapter": "openai_compatible",
                                                                   "root": "https://provider.invalid/v1"})
        assert provider.status_code == 201
        identity = provider.json()["id"]
        added = await client.post(f"/api/service/providers/{identity}/credentials", json={"secret": "synthetic-provider-secret"})
        assert added.status_code == 201
        assert "synthetic-provider-secret" not in (await client.get("/api/service/credentials")).text
        profile = await client.post("/api/service/proxies", json={"name": "Kiot", "mode": "kiotproxy",
                                                                "entries_text": "synthetic-kiot-secret"})
        assert profile.status_code == 201, profile.text
        assert "synthetic-kiot-secret" not in (await client.get("/api/service/proxies")).text


async def test_admin_mutations_require_csrf_and_validation_does_not_echo_input(pg_db):
    async with admin_env(pg_db) as (client, services):
        client.headers.update({"x-test-session": "synthetic-session"})
        assert (await client.post("/api/service/customers", json={"name": "X"})).status_code == 403
        client.headers.update({"x-csrf-token": "synthetic-csrf"})
        response = await client.post("/api/service/providers", json={"name": "X", "adapter": "openai_compatible",
                                                                    "root": "https://user:DO-NOT-ECHO@example.com"})
        assert response.status_code == 422
        assert "DO-NOT-ECHO" not in response.text


async def test_admin_names_and_versioned_model_proxy_updates(pg_db):
    async with admin_env(pg_db) as (client, services):
        client.headers.update({"x-test-session": "synthetic-session", "x-csrf-token": "synthetic-csrf"})
        customer = (await client.post("/api/service/customers", json={"name": "Before"})).json()["id"]
        edited = await client.patch(f"/api/service/customers/{customer}", json={"name": "After", "enabled": True, "version": 1})
        assert edited.status_code == 200, edited.text
        assert (await client.get("/api/service/customers")).json()[0]["name"] == "After"
        key = await client.post("/api/service/keys", json={"customer_id": customer, "name": "Production key", "policy": {}})
        assert key.status_code == 201, key.text
        assert (await client.get("/api/service/keys")).json()[0]["name"] == "Production key"
        model = {"model_id": "m", "identity": "model-m", "enabled": True}
        assert (await client.post("/api/service/models", json=model)).status_code == 201
        changed = await client.put("/api/service/models/m/1", json=dict(model, input_micro=2_000_000))
        assert changed.status_code == 200, changed.text
        assert (await client.put("/api/service/models/m/1", json=model)).status_code == 409
        profile = (await client.post("/api/service/proxies", json={"name": "Direct", "mode": "direct"})).json()["id"]
        assert (await client.put(f"/api/service/proxies/{profile}/1", json={"name": "Pool", "mode": "pool", "entries_text": "proxy.invalid:80"})).status_code == 200
        assert (await client.put(f"/api/service/proxies/{profile}/1", json={"name": "Old", "mode": "direct"})).status_code == 409


async def test_admin_can_disable_provider_and_credential_without_erasing_records(pg_db):
    async with admin_env(pg_db) as (client, services):
        client.headers.update({"x-test-session": "synthetic-session", "x-csrf-token": "synthetic-csrf"})
        provider = (await client.post("/api/service/providers", json={"name": "x", "adapter": "openai_compatible",
            "root": "https://provider.invalid/v1"})).json()["id"]
        credential = (await client.post(f"/api/service/providers/{provider}/credentials", json={"secret": "synthetic"})).json()["id"]
        changed = await client.patch(f"/api/service/credentials/{credential}", json={"version": 1, "enabled": False})
        assert changed.status_code == 200, changed.text
        assert (await client.get("/api/service/credentials")).json()[0]["enabled"] is False
        result = await client.request("DELETE", f"/api/service/providers/{provider}", json={"version": 1})
        assert result.status_code == 200
        assert (await client.get("/api/service/providers")).json()[0]["config"]["enabled"] is False


async def test_admin_quota_round_trip_keeps_large_decimal_integers(pg_db):
    async with admin_env(pg_db) as (client, services):
        client.headers.update({"x-test-session":"synthetic-session","x-csrf-token":"synthetic-csrf"})
        customer = (await client.post('/api/service/customers',json={'name':'Precision'})).json()['id']
        created = await client.post('/api/service/keys',json={'customer_id':customer,'policy':{'total_micro':'100000000000000123456'}})
        assert created.status_code == 201, created.text
        result = (await client.get('/api/service/keys')).json()[0]
        assert result['policy']['total_micro'] == '100000000000000123456'


async def test_provider_discovery_stages_names_without_publishing(pg_db):
    import httpx
    from autobuild_json.gateway.transport.http import Transport
    from autobuild_json.gateway.transport.egress import EgressPolicy
    async with admin_env(pg_db) as (client, services):
        client.headers.update({'x-test-session':'synthetic-session','x-csrf-token':'synthetic-csrf'})
        async def resolver(host,port): return ['93.184.216.34']
        calls=[]
        def respond(request):
            calls.append(request)
            return httpx.Response(200,json={'data':[{'id':'model-one'}]})
        services.transport=Transport(EgressPolicy(resolver=resolver),adapter=httpx.MockTransport(respond))
        provider=(await client.post('/api/service/providers',json={'name':'P','adapter':'openai_compatible','root':'https://provider.invalid/v1'})).json()['id']
        await client.post(f'/api/service/providers/{provider}/credentials',json={'secret':'synthetic-secret'})
        result=await client.post(f'/api/service/providers/{provider}/discover',json={})
        assert result.status_code==200, result.text
        assert result.json()['models']==['model-one']
        assert (await client.get('/api/service/models')).json()==[]
        assert len(calls)==1 and calls[0].headers['authorization']=='Bearer synthetic-secret'


async def test_key_rename_and_exact_model_coefficients(pg_db):
    async with admin_env(pg_db) as (client,services):
        client.headers.update({'x-test-session':'synthetic-session','x-csrf-token':'synthetic-csrf'})
        owner=(await client.post('/api/service/customers',json={'name':'Owner'})).json()['id']
        key=(await client.post('/api/service/keys',json={'customer_id':owner,'name':'old','policy':{}})).json()['key_id']
        response=await client.patch(f'/api/service/keys/{key}',json={'version':1,'name':'new','policy':{}})
        assert response.status_code==200 and response.json()['name']=='new'
        model=await client.post('/api/service/models',json={'model_id':'m','identity':'m','input_micro':'1000001','output_micro':'2000000'})
        assert model.status_code==201,model.text
        assert (await client.get('/api/service/models')).json()[0]['input_micro']=='1000001'


async def test_alias_budget_and_safe_proxy_deletion(pg_db):
    async with admin_env(pg_db) as (client,services):
        client.headers.update({'x-test-session':'synthetic-session','x-csrf-token':'synthetic-csrf'})
        await client.post('/api/service/models',json={'model_id':'m','identity':'m'})
        result=await client.post('/api/service/aliases',json={'alias':'friendly','model_id':'m'})
        assert result.status_code==201,result.text
        assert (await client.get('/api/service/aliases')).json()==[{'alias':'friendly','model_id':'m'}]
        budget=await client.post('/api/service/budgets',json={'currency':'USD','limit':'12.25'})
        assert budget.status_code==201,budget.text
        values=(await client.get('/api/service/budgets')).json()
        assert values[0]['budget_limit']=='12.250000000000'
        profile=(await client.post('/api/service/proxies',json={'name':'direct','mode':'direct'})).json()['id']
        assert (await client.request('DELETE',f'/api/service/proxies/{profile}',json={'version':1})).status_code==200
        assert (await client.get('/api/service/proxies')).json()==[]
