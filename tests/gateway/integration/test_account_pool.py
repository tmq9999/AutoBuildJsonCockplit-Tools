import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.identity.policy import KeyPolicy
from autobuild_json.gateway.identity.service import IdentityService
from autobuild_json.gateway.providers.catalog import Catalog
from autobuild_json.gateway.routing.pool_records import AccountPoolPolicy, PoolMember, PoolScope
from autobuild_json.gateway.routing.records import BindingConfig, ModelConfig, ProviderConfig
from autobuild_json.gateway.secrets import Vault
from autobuild_json.oauth import ISSUER

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def pool_case(db):
    catalog = Catalog(db, Vault({"v1": b"a" * 32}, active="v1"))
    provider = await catalog.create_provider(ProviderConfig(name="fixture", adapter="codex_oauth", auth_mode="oauth",
        root="https://chatgpt.com/backend-api/codex", wire_api="responses"))
    await catalog.put_model(ModelConfig(model_id="public", identity="identity", enabled=True))
    for _ in range(2):
        credential = await catalog.put_credential(provider, "synthetic-fixture")
        async with db.sessions.begin() as session:
            await session.execute(text("UPDATE credentials SET issuer=:issuer,account_id=:account WHERE id=:id"),
                                  {"issuer": ISSUER, "account": str(credential), "id": credential})
        await catalog.put_binding(BindingConfig(uuid4(), provider, credential, "public", "upstream", "identity", 100, 100))
    identities = IdentityService(db, b"p" * 32)
    owner = await identities.create_customer("Pool fixture")
    key = await identities.create_key(owner, KeyPolicy(model_ids={"public"}, protocols={"openai"}))
    principal = await identities.authenticate(key.secret, "openai")
    request = SimpleNamespace(model="public", required_capabilities={"text"})
    routes = await catalog.candidates(principal, request)
    scope = PoolScope(customer_id=owner, key_id=key.key_id, model_id="public",
                      session_digest=hashlib.sha256(b"same-session").digest())
    return catalog, identities, principal, request, routes, scope


def store(db, key=b"s" * 32):
    from autobuild_json.gateway.routing.pool_store import PoolStore
    return PoolStore(db, scope_key=key)


def policy(routes):
    return AccountPoolPolicy(members=tuple(PoolMember(credential_id=r.credential_id) for r in routes))


def expiry():
    return datetime.now(timezone.utc) + timedelta(hours=48)


async def test_policy_cas_and_audit_are_atomic(pg_db):
    _, _, _, _, routes, _ = await pool_case(pg_db)
    pools = store(pg_db)
    config = policy(routes)
    assert await pools.get("public") is None
    assert await pools.put("public", None, config, "fixture-admin") == 1
    assert await pools.get("public") == (1, config)
    outcomes = await asyncio.gather(*(pools.put("public", 1, config, "fixture-admin") for _ in range(2)),
                                    return_exceptions=True)
    assert outcomes.count(2) == 1
    assert [item.code for item in outcomes if isinstance(item, GatewayError)] == ["version_conflict"]
    with pytest.raises(GatewayError, match="version_conflict"):
        await pools.put("public", None, config, "fixture-admin")
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='codex.pool.updated'")) == 2


@pytest.mark.parametrize("mutation", ["disabled_credential", "disabled_provider", "foreign_member", "disabled_binding",
                                      "wrong_identity", "noncodex", "foreign_issuer", "alias"])
async def test_policy_rejects_disabled_foreign_or_wrong_binding(pg_db, mutation):
    catalog, _, _, _, routes, _ = await pool_case(pg_db)
    pools = store(pg_db)
    config, model = policy(routes), "public"
    sql = {"disabled_credential": "UPDATE credentials SET enabled=false", "disabled_provider":
           "UPDATE providers SET config=jsonb_set(config,'{enabled}','false')", "disabled_binding":
           "UPDATE model_bindings SET config=jsonb_set(config,'{enabled}','false')", "wrong_identity":
           "UPDATE model_bindings SET config=jsonb_set(config,'{identity}','\"wrong\"')", "noncodex":
           "UPDATE providers SET config=jsonb_set(config,'{adapter}','\"openai_compatible\"')", "foreign_issuer":
           "UPDATE credentials SET issuer='https://wrong.invalid'"}
    if mutation in sql:
        async with pg_db.sessions.begin() as session:
            await session.execute(text(sql[mutation]))
    elif mutation == "foreign_member":
        config = AccountPoolPolicy(members=(PoolMember(credential_id=uuid4()),))
    else:
        await catalog.put_alias("alias", "public")
        model = "alias"
    with pytest.raises(GatewayError, match="invalid_request"):
        await pools.put(model, None, config, "fixture-admin")
    assert await pools.get("public") is None
    if mutation == "alias":
        with pytest.raises(GatewayError, match="invalid_request"):
            await pools.get("alias")


async def test_same_session_first_writer_wins_and_binding_ttl_is_capped(pg_db):
    _, _, _, _, routes, scope = await pool_case(pg_db)
    pools = store(pg_db)
    await pools.put("public", None, policy(routes), "fixture-admin")
    winners = await asyncio.gather(*(pools.bind(scope, routes[i % 2], expiry()) for i in range(8)))
    assert len({r.credential_id for r in winners}) == 1
    assert await pools.resolve(scope) == winners[0]
    async with pg_db.sessions() as session:
        row = (await session.execute(text("SELECT *,expires_at<=clock_timestamp()+interval '24 hours' AS capped "
                                          "FROM account_session_bindings"))).mappings().one()
    assert row["capped"] and row["version"] == 1
    assert len(row["scope_digest"]) == 32 and row["scope_digest"] != scope.session_digest
    assert b"same-session" not in row["scope_digest"]
    assert await store(pg_db, b"t" * 32).resolve(scope) is None


async def test_same_session_across_customer_key_and_model_is_isolated(pg_db):
    catalog, identities, _, _, routes, scope = await pool_case(pg_db)
    pools = store(pg_db)
    await pools.bind(scope, routes[0], expiry())
    key = await identities.create_key(scope.customer_id, KeyPolicy(model_ids={"public"}, protocols={"openai"}))
    second_key = scope.model_copy(update={"key_id": key.key_id})
    owner = await identities.create_customer("Other pool fixture")
    other_key = await identities.create_key(owner, KeyPolicy(model_ids={"public"}, protocols={"openai"}))
    second_owner = scope.model_copy(update={"customer_id": owner, "key_id": other_key.key_id})
    for other in (second_key, second_owner):
        assert await pools.resolve(other) is None
        assert (await pools.bind(other, routes[1], expiry())).credential_id == routes[1].credential_id
    await catalog.put_model(ModelConfig(model_id="other", identity="identity", enabled=True))
    await catalog.put_binding(BindingConfig(uuid4(), routes[0].provider_id, routes[0].credential_id,
                                           "other", "upstream", "identity", 100, 100))
    # Give the original key model permission without changing its identity.
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE api_keys SET policy=jsonb_set(policy,'{model_ids}','[\"public\",\"other\"]') "
                                   "WHERE id=:id"), {"id": scope.key_id})
    other_model = scope.model_copy(update={"model_id": "other"})
    assert await pools.resolve(other_model) is None
    from autobuild_json.gateway.identity.policy import Principal
    other_route = (await catalog.candidates(Principal(scope.customer_id, scope.key_id, 1),
                   SimpleNamespace(model="other", required_capabilities={"text"})))[0]
    await pools.bind(other_model, other_route, expiry())
    assert (await pools.resolve(scope)).credential_id == routes[0].credential_id
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(DISTINCT scope_digest) FROM account_session_bindings")) == 4


async def test_expired_optional_affinity_can_rebind_but_live_affinity_cannot(pg_db):
    _, _, _, _, routes, scope = await pool_case(pg_db)
    pools = store(pg_db)
    await pools.bind(scope, routes[0], expiry())
    assert (await pools.bind(scope, routes[1], expiry())).credential_id == routes[0].credential_id
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE account_session_bindings SET expires_at=clock_timestamp()-interval '1 second'"))
    assert await pools.resolve(scope) is None
    assert (await pools.bind(scope, routes[1], expiry())).credential_id == routes[1].credential_id
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT version FROM account_session_bindings")) == 2


@pytest.mark.parametrize("mutation", ["disabled", "cooldown", "binding_changed", "policy_denied", "key_revoked"])
async def test_unavailable_live_pin_never_fails_over(pg_db, mutation):
    _, _, _, _, routes, scope = await pool_case(pg_db)
    pools = store(pg_db)
    await pools.bind(scope, routes[0], expiry())
    sql = {"disabled": "UPDATE credentials SET enabled=false WHERE id=:credential", "cooldown":
           "UPDATE credentials SET cooldown_until=clock_timestamp()+interval '1 minute' WHERE id=:credential",
           "binding_changed": "UPDATE model_bindings SET credential_id=:other WHERE id=:binding",
           "policy_denied": "UPDATE api_keys SET policy=jsonb_set(policy,'{model_ids}','[]') WHERE id=:key",
           "key_revoked": "UPDATE api_keys SET revoked_at=clock_timestamp() WHERE id=:key"}
    async with pg_db.sessions.begin() as session:
        await session.execute(text(sql[mutation]), {"credential": routes[0].credential_id, "other": routes[1].credential_id,
                                                    "binding": routes[0].binding_id, "key": scope.key_id})
    for operation in (pools.resolve(scope), pools.bind(scope, routes[1], expiry())):
        with pytest.raises(GatewayError, match="invalid_state"):
            await operation


async def test_malformed_or_mismatched_external_scope_and_route_rejected(pg_db):
    catalog, _, _, _, routes, scope = await pool_case(pg_db)
    pools = store(pg_db)
    for wrong in (scope.model_copy(update={"customer_id": uuid4()}), scope.model_copy(update={"session_digest": None}),
                  scope.model_copy(update={"model_id": "missing"})):
        with pytest.raises(GatewayError, match="invalid_state"):
            await pools.bind(wrong, routes[0], expiry())
    await catalog.put_alias("alias", "public")
    with pytest.raises(GatewayError, match="invalid_state"):
        await pools.resolve(scope.model_copy(update={"model_id": "alias"}))
    with pytest.raises(GatewayError, match="invalid_state"):
        await pools.bind(scope, replace(routes[0], credential_id=routes[1].credential_id), expiry())
    with pytest.raises(GatewayError, match="invalid_state"):
        await pools.bind(scope, routes[0], datetime.now())
    with pytest.raises(GatewayError, match="invalid_state"):
        await pools.bind(scope, routes[0], datetime.now(timezone.utc)-timedelta(seconds=1))


async def test_pool_metadata_cannot_authorize_a_model_and_no_policy_keeps_round_robin(pg_db):
    from autobuild_json.gateway.routing.account_pool import order_candidates
    catalog, _, principal, request, routes, _ = await pool_case(pg_db)
    pools = store(pg_db)
    first = await catalog.account_candidates(principal, request)
    second = await catalog.account_candidates(principal, request)
    now = datetime.now(timezone.utc)
    assert order_candidates(None, first, now, None)[0].binding_id != order_candidates(None, second, now, None)[0].binding_id
    await pools.put("public", None, policy(routes), "fixture-admin")
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE api_keys SET policy=jsonb_set(policy,'{model_ids}','[]')"))
    with pytest.raises(GatewayError, match="permission_denied"):
        await catalog.account_candidates(principal, request)


async def test_empty_auto_pool_is_persisted_explicit_pause_not_legacy_fallback(pg_db):
    from autobuild_json.gateway.routing.account_pool import order_candidates
    catalog, _, principal, request, routes, scope = await pool_case(pg_db)
    pools = store(pg_db)
    candidates = await catalog.account_candidates(principal, request)
    assert len(candidates) == 2
    assert len(order_candidates(None, candidates, datetime.now(timezone.utc), None)) == 2

    assert await pools.put("public", None, AccountPoolPolicy(mode="auto", members=()), "fixture-admin") == 1
    version, saved = await pools.get("public")
    assert version == 1 and saved.mode == "auto" and saved.members == ()
    assert order_candidates(saved, candidates, datetime.now(timezone.utc), None) == ()
    with pytest.raises(GatewayError, match="invalid_state"):
        await pools.bind(scope, routes[0], expiry())
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM account_session_bindings")) == 0


async def test_empty_auto_pool_can_pause_enabled_canonical_model_without_bindings(pg_db):
    catalog, _, _, _, _, _ = await pool_case(pg_db)
    await catalog.put_model(ModelConfig(model_id="unbound", identity="identity", enabled=True))
    pools = store(pg_db)
    config = AccountPoolPolicy(mode="auto", members=())
    assert await pools.put("unbound", None, config, "fixture-admin") == 1
    assert await pools.get("unbound") == (1, config)


@pytest.mark.parametrize("empty", [True, False])
async def test_disabled_canonical_model_rejects_empty_and_nonempty_new_policy(pg_db, empty):
    catalog, _, _, _, routes, _ = await pool_case(pg_db)
    await catalog.put_model(ModelConfig(model_id="public", identity="identity", enabled=False))
    pools = store(pg_db)
    config = AccountPoolPolicy(mode="auto", members=()) if empty else policy(routes)
    with pytest.raises(GatewayError, match="invalid_request"):
        await pools.put("public", None, config, "fixture-admin")
    assert await pools.get("public") is None
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='codex.pool.updated'")) == 0
