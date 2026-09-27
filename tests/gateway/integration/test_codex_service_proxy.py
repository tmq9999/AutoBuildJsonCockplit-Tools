"""Private Codex default egress changes share existing proxy policy/history."""
import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from autobuild_json.gateway.accounts.imports import CODEX_PROVIDER
from autobuild_json.gateway.accounts.proxy_selection import AccountProxyResolver
from autobuild_json.gateway.admin.schemas import ProxyInput
from autobuild_json.gateway.proxy.profiles import ProfileStore
from tests.gateway.codex_admin_support import ORIGIN, account, private_env

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
PATH = "/api/service/codex-service/proxy"


async def test_service_proxy_is_private_and_requires_origin_csrf(pg_db, settings):
    from autobuild_json.gateway.http.app import create_gateway_app

    command = {"version": 1, "proxy_profile_id": None}
    async with private_env(pg_db, settings, login=False) as (client, services):
        assert (await client.get(PATH)).status_code == 401
        assert (await client.put(PATH, headers={"Origin": ORIGIN}, json=command)).status_code == 401
        login = await client.post("/api/session", headers={"Origin": ORIGIN}, json={"token": "test-admin-token"})
        assert (await client.put(PATH, headers={"Origin": ORIGIN}, json=command)).status_code == 403
        csrf = login.json()["csrf_token"]
        assert (await client.put(PATH, headers={"Origin": "http://evil.invalid", "X-CSRF-Token": csrf},
                                 json=command)).status_code == 403
        bad = await client.get(PATH + "?token=DO-NOT-ECHO")
        assert bad.status_code == 400 and "DO-NOT-ECHO" not in bad.text
        public = create_gateway_app(services.identity, services.catalog, services.engine, allowed_hosts=("public",))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="http://public") as other:
            assert (await other.get(PATH)).status_code == 404
            assert (await other.put(PATH, json=command)).status_code == 404
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM audit_events")) == 0


async def test_missing_or_non_codex_provider_returns_404_without_creating_config(pg_db, settings):
    async with private_env(pg_db, settings) as (client, services):
        assert (await client.get(PATH)).status_code == 404
        assert (await client.put(PATH, json={"version": 1, "proxy_profile_id": None})).status_code == 404
        async with pg_db.sessions() as session:
            for table in ("providers", "config_versions", "audit_events"):
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        await account(services)
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE providers SET config=jsonb_set(config,'{adapter}','\"openai_compatible\"') "
                                       "WHERE id=:id"), {"id": CODEX_PROVIDER})
        assert (await client.get(PATH)).status_code == 404
        assert (await client.put(PATH, json={"version": 1, "proxy_profile_id": None})).status_code == 404


@pytest.mark.parametrize("mode,entries", [
    ("direct", ""),
    ("fixed", "http://fixture-user:DO-NOT-ECHO@proxy.example:8080"),
    ("pool", "http://fixture-user:DO-NOT-ECHO@proxy.example:8080\nhttp://other.example:8081"),
    ("kiotproxy", "DO-NOT-ECHO-KIOT-KEY\nSECOND-SYNTHETIC-KIOT-KEY"),
])
async def test_all_profile_modes_are_storage_only_and_safe(pg_db, settings, mode, entries):
    async with private_env(pg_db, settings) as (client, services):
        credential = await account(services)
        initial = await client.get(PATH)
        assert initial.status_code == 200
        assert initial.json() == {"provider_id": str(CODEX_PROVIDER), "version": 1,
            "proxy_profile_id": None, "profile": None, "source": "direct"}
        assert "no-store" in initial.headers["cache-control"].split(", ")
        profile = await services.profiles.create(ProxyInput(name="Operator profile", mode=mode,
                                                          entries_text=SecretStr(entries)))
        command = {"version": 1, "proxy_profile_id": str(profile)}
        changed = await client.put(PATH, json=command)
        assert changed.status_code == 200, changed.text
        assert changed.json()["version"] == 2
        stored = await client.get(PATH)
        assert stored.json() == {"provider_id": str(CODEX_PROVIDER), "version": 2,
            "proxy_profile_id": str(profile), "profile": {"id": str(profile), "name": "Operator profile", "mode": mode},
            "source": "provider"}
        policy, stamp = await AccountProxyResolver(pg_db, services.profiles).policy(credential)
        assert policy.mode == mode and stamp["proxy_id"] == str(profile)
        for forbidden in ("DO-NOT-ECHO", "fixture-user", "proxy.example", "SECOND-SYNTHETIC", "encrypted_entries"):
            assert forbidden not in changed.text + stored.text
        async with pg_db.sessions() as session:
            audit = (await session.execute(text("SELECT actor,record_id,details FROM audit_events "
                "WHERE action='codex.service_proxy.updated'"))).mappings().one()
            assert audit == {"actor": "admin", "record_id": CODEX_PROVIDER,
                             "details": {"version": 2, "proxy_profile_id": str(profile)}}
            assert await session.scalar(text("SELECT count(*) FROM requests")) == 0


async def test_proxy_policy_does_not_nested_checkout_under_small_pool(pg_db, settings):
    """Concurrent policy reads must not deadlock on resolver/profile sessions."""
    from pydantic import SecretStr
    from autobuild_json.gateway.storage.db import make_database

    async with private_env(pg_db, settings) as (_client, services):
        credential = await account(services)
        profile = await services.profiles.create(
            ProxyInput(name="Pool regression", mode="fixed",
                       entries_text=SecretStr("http://proxy.example:8080")))
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE credentials SET profile_id=:profile WHERE id=:id"),
                                  {"id": credential, "profile": profile})
        async with pg_db.sessions() as session:
            schema = await session.scalar(text("SELECT current_schema()"))
        small = make_database(SecretStr(pg_db.engine.url.render_as_string(hide_password=False)),
                              schema=schema, pool_size=3, max_overflow=0, pool_timeout=0.2)
        try:
            profiles = ProfileStore(small, services.vault, b"p" * 32)
            for loader in (profiles, profiles.load):
                resolver = AccountProxyResolver(small, loader)
                policies = await asyncio.gather(*(resolver.policy(credential) for _ in range(3)))
                assert [policy.mode for policy, _stamp in policies] == ["fixed"] * 3
        finally:
            await small.close()


async def test_proxy_update_preserves_raw_config_and_versions_exactly(pg_db, settings):
    async with private_env(pg_db, settings) as (client, services):
        await account(services)
        # Missing optional defaults and a normalizable trailing slash must stay
        # unchanged: the endpoint does not own these provider settings.
        raw = {"name": "Private upstream", "adapter": "codex_oauth", "auth_mode": "oauth",
               "root": "https://chatgpt.com/backend-api/codex/", "timeout": 217, "rpm_limit": 177,
               "concurrency_limit": 9, "enabled": False}
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE providers SET config=CAST(:config AS jsonb) WHERE id=:id"),
                                  {"id": CODEX_PROVIDER, "config": json.dumps(raw)})
        profile = await services.profiles.create(ProxyInput(name="Chosen", mode="direct"))
        changed = await client.put(PATH, json={"version": 1, "proxy_profile_id": str(profile)})
        assert changed.status_code == 200, changed.text
        async with pg_db.sessions() as session:
            stored = await session.scalar(text("SELECT config FROM providers WHERE id=:id"), {"id": CODEX_PROVIDER})
            history = await session.scalar(text("SELECT config FROM config_versions WHERE provider_id=:id AND version=2"),
                                           {"id": CODEX_PROVIDER})
        assert stored == history == dict(raw, proxy_profile_id=str(profile))
        cleared = await client.put(PATH, json={"version": 2, "proxy_profile_id": None})
        assert cleared.status_code == 200 and cleared.json()["source"] == "direct"
        async with pg_db.sessions() as session:
            stored = await session.scalar(text("SELECT config FROM providers WHERE id=:id"), {"id": CODEX_PROVIDER})
            history = await session.scalar(text("SELECT config FROM config_versions WHERE provider_id=:id AND version=3"),
                                           {"id": CODEX_PROVIDER})
        assert stored == history == dict(raw, proxy_profile_id=None)


async def test_override_precedence_and_clearing_default(pg_db, settings):
    async with private_env(pg_db, settings) as (client, services):
        credential = await account(services)
        default = await services.profiles.create(ProxyInput(name="Default fixed", mode="fixed",
            entries_text=SecretStr("http://proxy.example:8080")))
        override = await services.profiles.create(ProxyInput(name="Credential direct", mode="direct"))
        resolver = AccountProxyResolver(pg_db, services.profiles)
        policy, stamp = await resolver.policy(credential)
        assert policy.mode == "direct" and stamp["proxy_id"] is None
        assert (await client.put(PATH, json={"version": 1, "proxy_profile_id": str(default)})).status_code == 200
        policy, stamp = await resolver.policy(credential)
        assert policy.mode == "fixed" and stamp["proxy_id"] == str(default)
        path = f"/api/service/oauth-accounts/{credential}"
        assert (await client.patch(path, json={"version": 1, "enabled": True,
                                               "proxy_profile_id": str(override)})).status_code == 200
        policy, stamp = await resolver.policy(credential)
        assert policy.mode == "direct" and stamp["proxy_id"] == str(override)
        assert (await client.put(PATH, json={"version": 2, "proxy_profile_id": None})).status_code == 200
        policy, stamp = await resolver.policy(credential)
        assert policy.mode == "direct" and stamp["proxy_id"] == str(override)
        assert (await client.patch(path, json={"version": 2, "enabled": True,
                                               "proxy_profile_id": None})).status_code == 200
        policy, stamp = await resolver.policy(credential)
        assert policy.mode == "direct" and stamp["proxy_id"] is None


async def test_invalid_and_stale_commands_do_not_mutate_or_echo_input(pg_db, settings):
    async with private_env(pg_db, settings) as (client, services):
        await account(services)
        cases = [({"version": 1, "proxy_profile_id": str(uuid4())}, 422),
                 ({"version": 2, "proxy_profile_id": None}, 409),
                 ({"version": True, "proxy_profile_id": None}, 422),
                 ({"version": "1", "proxy_profile_id": None}, 422),
                 ({"version": 1, "proxy_profile_id": "DO-NOT-ECHO"}, 422),
                 ({"version": 1, "proxy_profile_id": None, "token": "DO-NOT-ECHO"}, 422),
                 ({"version": 1}, 422)]
        for command, code in cases:
            result = await client.put(PATH, json=command)
            assert result.status_code == code, result.text
            assert "DO-NOT-ECHO" not in result.text
        assert (await client.get(PATH)).json()["version"] == 1
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM config_versions WHERE provider_id=:id"),
                                        {"id": CODEX_PROVIDER}) == 1
            assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='codex.service_proxy.updated'")) == 0


async def test_concurrent_cas_has_one_winner_and_one_history(pg_db, settings):
    async with private_env(pg_db, settings) as (client, services):
        await account(services)
        profiles = [await services.profiles.create(ProxyInput(name=f"Profile {i}", mode="direct")) for i in range(2)]
        results = await asyncio.gather(*(client.put(PATH, json={"version": 1, "proxy_profile_id": str(profile)})
                                        for profile in profiles))
        assert sorted(r.status_code for r in results) == [200, 409]
        winner = next(r for r in results if r.status_code == 200).json()["proxy_profile_id"]
        assert (await client.get(PATH)).json()["proxy_profile_id"] == winner
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM config_versions WHERE provider_id=:id"),
                                        {"id": CODEX_PROVIDER}) == 2
            assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='codex.service_proxy.updated'")) == 1


async def test_switching_default_does_not_lock_old_profile(pg_db, settings):
    async with private_env(pg_db, settings) as (client, services):
        await account(services)
        first = await services.profiles.create(ProxyInput(name="Old", mode="direct"))
        second = await services.profiles.create(ProxyInput(name="New", mode="direct"))
        assert (await client.put(PATH, json={"version": 1, "proxy_profile_id": str(first)})).status_code == 200
        async with pg_db.sessions.begin() as session:
            await session.execute(text("SELECT id FROM proxy_profiles WHERE id=:id FOR UPDATE"), {"id": first})
            result = await asyncio.wait_for(client.put(PATH, json={"version": 2, "proxy_profile_id": str(second)}), 2)
            assert result.status_code == 200


async def test_default_reference_and_proxy_delete_serialize(pg_db, settings, monkeypatch):
    async with private_env(pg_db, settings) as (client, services):
        await account(services)
        profile = await services.profiles.create(ProxyInput(name="Chosen", mode="direct"))
        entered, release = asyncio.Event(), asyncio.Event()
        real_audit = services.identity._audit

        async def audit_gate(*args, **kwargs):
            await real_audit(*args, **kwargs)
            entered.set()
            await release.wait()

        monkeypatch.setattr(services.identity, "_audit", audit_gate)
        change = asyncio.create_task(client.put(PATH, json={"version": 1, "proxy_profile_id": str(profile)}))
        await asyncio.wait_for(entered.wait(), 3)
        delete = asyncio.create_task(client.request("DELETE", f"/api/service/proxies/{profile}", json={"version": 1}))

        async def blocked_or_finished():
            while not delete.done():
                async with pg_db.sessions() as session:
                    if await session.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                        "WHERE cardinality(pg_blocking_pids(pid))>0 AND query LIKE '%proxy_profiles%')")):
                        return
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(blocked_or_finished(), 3)
        finally:
            release.set()
        changed, deleted = await asyncio.wait_for(asyncio.gather(change, delete), 3)
        assert changed.status_code == 200 and deleted.status_code == 409
        assert (await client.get(PATH)).json()["proxy_profile_id"] == str(profile)
