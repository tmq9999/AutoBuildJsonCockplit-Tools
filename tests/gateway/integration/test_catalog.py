from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def catalog_case(pg_db):
    from autobuild_json.gateway.secrets import Vault
    from autobuild_json.gateway.providers.catalog import Catalog
    from autobuild_json.gateway.routing.records import ProviderConfig, ModelConfig, BindingConfig
    from autobuild_json.gateway.identity.policy import KeyPolicy
    from autobuild_json.gateway.identity.service import IdentityService
    vault = Vault({"v1": b"a" * 32}, active="v1")
    catalog = Catalog(pg_db, vault)
    provider = await catalog.create_provider(ProviderConfig(name="Provider", adapter="openai_compatible",
                                                            root="https://provider.invalid/v1"))
    credential = await catalog.put_credential(provider, "synthetic-upstream-secret")
    await catalog.put_model(ModelConfig(model_id="public", identity="identity-A", enabled=True))
    binding = BindingConfig(id=uuid4(), provider_id=provider, credential_id=credential, public_model_id="public",
                            upstream_model="upstream", identity="identity-A", input_bound=1000, output_bound=500)
    await catalog.put_binding(binding)
    identity = IdentityService(pg_db, b"p" * 32)
    owner = await identity.create_customer("Catalog test")
    issued = await identity.create_key(owner, KeyPolicy(model_ids={"public"}, protocols={"openai"}))
    principal = await identity.authenticate(issued.secret, "openai")
    return catalog, principal, provider, credential, binding


async def test_routes_are_immutable_and_credentials_stay_encrypted(pg_db):
    from sqlalchemy import text
    catalog, principal, _, _, _ = await catalog_case(pg_db)
    routes = await catalog.candidates(principal, SimpleNamespace(model="public", required_capabilities={"text"}))
    assert len(routes) == 1 and routes[0].upstream_model == "upstream"
    assert "synthetic-upstream-secret" not in repr(routes)
    with pytest.raises(FrozenInstanceError):
        routes[0].root = "https://other.invalid"
    async with pg_db.sessions() as session:
        stored = await session.scalar(text("SELECT encrypted_secret::text FROM credentials LIMIT 1"))
    assert "synthetic-upstream-secret" not in stored


async def test_alias_resolves_then_checks_permissions(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.routing.records import ModelConfig
    catalog, principal, _, _, _ = await catalog_case(pg_db)
    await catalog.put_model(ModelConfig(model_id="private", identity="identity-P", enabled=True))
    await catalog.put_alias("shortcut", "private")
    with pytest.raises(GatewayError, match="permission_denied"):
        await catalog.candidates(principal, SimpleNamespace(model="shortcut", required_capabilities={"text"}))
    assert [item.model_id for item in await catalog.list_models(principal)] == ["public"]
    with pytest.raises(GatewayError):
        await catalog.put_alias("private", "shortcut")


async def test_round_robin_and_provider_cooldown(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    from dataclasses import replace
    catalog, principal, provider, credential, binding = await catalog_case(pg_db)
    await catalog.put_binding(replace(binding, id=uuid4(), upstream_model="upstream-2"))
    request = SimpleNamespace(model="public", required_capabilities={"text"})
    first, second = await catalog.candidates(principal, request), await catalog.candidates(principal, request)
    assert first[0].binding_id != second[0].binding_id
    await catalog.cooldown(provider, None, 60)
    with pytest.raises(GatewayError, match="upstream_unavailable"):
        await catalog.candidates(principal, request)


async def test_config_edit_changes_new_routes_not_inflight_snapshot(pg_db):
    from autobuild_json.gateway.routing.records import ProviderConfig
    from autobuild_json.gateway.errors import GatewayError
    catalog, principal, provider, _, _ = await catalog_case(pg_db)
    request = SimpleNamespace(model="public", required_capabilities={"text"})
    old = (await catalog.candidates(principal, request))[0]
    await catalog.update_provider(provider, 1, ProviderConfig(name="Updated", adapter="openai_compatible",
                                                            root="https://next.invalid/v1"))
    new = (await catalog.candidates(principal, request))[0]
    assert old.root == "https://provider.invalid/v1"
    assert new.root == "https://next.invalid/v1" and new.config_version == 2
    with pytest.raises(GatewayError, match="version_conflict"):
        await catalog.update_provider(provider, 1, ProviderConfig(name="Stale", adapter="openai_compatible",
                                                                root="https://bad.invalid/v1"))


async def test_disabled_credential_and_missing_capability_are_not_routed(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    catalog, principal, _, credential, _ = await catalog_case(pg_db)
    with pytest.raises(GatewayError, match="unsupported_feature"):
        await catalog.candidates(principal, SimpleNamespace(model="public", required_capabilities={"vision"}))
    await catalog.set_credential_enabled(credential, False)
    with pytest.raises(GatewayError, match="upstream_unavailable"):
        await catalog.candidates(principal, SimpleNamespace(model="public", required_capabilities={"text"}))


async def test_discovery_does_not_publish_upstream_models(pg_db):
    catalog, principal, provider, _, _ = await catalog_case(pg_db)
    await catalog.record_discovery(provider, ["discovered-A", "discovered-B"])
    assert [item.model_id for item in await catalog.list_models(principal)] == ["public"]
    assert await catalog.discovered_models(provider) == ["discovered-A", "discovered-B"]


async def test_credential_cooldown_does_not_disable_another_credential(pg_db):
    from dataclasses import replace
    catalog, principal, provider, credential, binding = await catalog_case(pg_db)
    other = await catalog.put_credential(provider, "another-synthetic-key")
    await catalog.put_binding(replace(binding, id=uuid4(), credential_id=other))
    await catalog.cooldown(provider, credential, 60)
    routes = await catalog.candidates(principal, SimpleNamespace(model="public", required_capabilities={"text"}))
    assert [route.credential_id for route in routes] == [other]


async def test_model_identity_change_does_not_route_old_bindings(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.routing.records import ModelConfig
    catalog, principal, _, _, _ = await catalog_case(pg_db)
    await catalog.put_model(ModelConfig(model_id="public", identity="different-identity", enabled=True))
    with pytest.raises(GatewayError, match="upstream_unavailable"):
        await catalog.candidates(principal, SimpleNamespace(model="public", required_capabilities={"text"}))


async def test_credentials_rotate_without_touching_model_binding(pg_db):
    from sqlalchemy import text
    catalog, principal, _, credential, _ = await catalog_case(pg_db)
    await catalog.rotate_credential(credential, "new-synthetic")
    routes = await catalog.candidates(principal, SimpleNamespace(model="public", required_capabilities={"text"}))
    assert routes[0].credential_id == credential
    async with pg_db.sessions() as session:
        audit = (await session.execute(text("SELECT action FROM audit_events"))).scalars().all()
    assert "credential.rotated" in audit
