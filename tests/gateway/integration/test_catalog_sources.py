import pytest
from uuid import uuid4

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_source_context_contains_current_route_without_secret(pg_db):
    from tests.gateway.catalog_support import source_case

    sources, source = await source_case(pg_db)
    context = await sources.context(source.id)
    assert context.route.root == "https://provider.invalid/v1"
    assert context.route.credential_id == source.credential_id
    assert "synthetic-upstream-secret" not in repr(context)
    assert "ciphertext" not in context.model_dump_json()


async def test_source_update_is_versioned_and_controls_next_run(pg_db):
    from autobuild_json.gateway.catalog.records import SourceInput
    from autobuild_json.gateway.errors import GatewayError
    from tests.gateway.catalog_support import source_case

    sources, source = await source_case(pg_db)
    scheduled = await sources.update(
        source.id, 1,
        SourceInput(provider_id=source.provider_id, credential_id=source.credential_id,
                    mode=source.mode, schedule_enabled=True, interval_seconds=300),
        "test-admin",
    )
    assert scheduled.version == 2 and scheduled.next_run_at is not None
    assert scheduled.schedule_enabled is True
    with pytest.raises(GatewayError, match="version_conflict"):
        await sources.update(source.id, 1, SourceInput(provider_id=source.provider_id,
                                                      credential_id=source.credential_id, mode=source.mode), "test-admin")
    disabled = await sources.update(
        source.id, 2, SourceInput(provider_id=source.provider_id, credential_id=source.credential_id,
                                  mode=source.mode, enabled=False), "test-admin"
    )
    assert disabled.version == 3 and disabled.next_run_at is None
    assert [item.id for item in await sources.list()] == [source.id]
    with pytest.raises(GatewayError, match="invalid_state"):
        await sources.context(source.id)


async def test_source_rejects_wrong_mode_foreign_credential_and_identity_edit(pg_db):
    from autobuild_json.gateway.catalog.records import SourceInput
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.providers.catalog import Catalog
    from autobuild_json.gateway.routing.records import ProviderConfig
    from tests.gateway.catalog_support import source_case, synthetic_vault

    sources, source = await source_case(pg_db)
    with pytest.raises(GatewayError, match="invalid_request"):
        await sources.create(SourceInput(provider_id=source.provider_id, credential_id=source.credential_id,
                                         mode="gemini"), "test-admin")
    other = await Catalog(pg_db, synthetic_vault()).create_provider(
        ProviderConfig(name="Other", adapter="openai_compatible", root="https://other.invalid/v1")
    )
    with pytest.raises(GatewayError, match="invalid_request"):
        await sources.create(SourceInput(provider_id=other, credential_id=source.credential_id,
                                         mode="openai_single"), "test-admin")
    with pytest.raises(GatewayError, match="invalid_request"):
        await sources.update(source.id, 1, SourceInput(provider_id=other,
                                                       credential_id=source.credential_id,
                                                       mode="openai_single"), "test-admin")
    with pytest.raises(GatewayError, match="version_conflict"):
        await sources.create(SourceInput(provider_id=source.provider_id,
                                         credential_id=source.credential_id,
                                         mode="openai_single"), "test-admin")


async def test_context_digest_tracks_network_identity_but_not_labels_or_schedule(pg_db):
    from autobuild_json.gateway.catalog.records import SourceInput
    from autobuild_json.gateway.providers.catalog import Catalog
    from autobuild_json.gateway.routing.records import ProviderConfig
    from tests.gateway.catalog_support import source_case, synthetic_vault

    sources, source = await source_case(pg_db)
    catalog = Catalog(pg_db, synthetic_vault())
    first = await sources.context(source.id)
    await catalog.update_provider(source.provider_id, 1, ProviderConfig(
        name="Renamed", adapter="openai_compatible", root="https://provider.invalid/v1"))
    renamed = await sources.context(source.id)
    assert renamed.stamp.content_digest == first.stamp.content_digest
    assert renamed.stamp.provider_version == 2
    await sources.update(source.id, 1, SourceInput(provider_id=source.provider_id,
                                                   credential_id=source.credential_id,
                                                   mode=source.mode, schedule_enabled=True), "test-admin")
    scheduled = await sources.context(source.id)
    assert scheduled.stamp.content_digest == first.stamp.content_digest
    await catalog.rotate_credential(source.credential_id, "rotated-synthetic-secret")
    rotated = await sources.context(source.id)
    assert rotated.stamp.content_digest != first.stamp.content_digest
    await catalog.update_provider(source.provider_id, 2, ProviderConfig(
        name="Renamed", adapter="openai_compatible", root="https://changed.invalid/v1"))
    assert (await sources.context(source.id)).stamp.content_digest != rotated.stamp.content_digest


async def test_context_uses_credential_proxy_over_provider_and_rejects_missing_profile(pg_db):
    import json
    from sqlalchemy import text
    from autobuild_json.gateway.providers.catalog import Catalog
    from autobuild_json.gateway.routing.records import ProviderConfig
    from tests.gateway.catalog_support import source_case, synthetic_vault

    sources, source = await source_case(pg_db)
    provider_proxy, credential_proxy = uuid4(), uuid4()
    vault = synthetic_vault()
    async with pg_db.sessions.begin() as session:
        for identity in (provider_proxy, credential_proxy):
            cipher = vault.seal("proxy_profile", identity, b"http://127.0.0.1:8080").to_dict()
            await session.execute(text("INSERT INTO proxy_profiles(id,name,config,encrypted_entries) "
                                       "VALUES (:id,'synthetic',CAST(:config AS jsonb),CAST(:cipher AS jsonb))"),
                                  {"id": identity, "config": json.dumps({"mode": "fixed", "region": "random",
                                                                            "protocol": "http", "rotate": False}),
                                   "cipher": json.dumps(cipher)})
        await session.execute(text("UPDATE credentials SET profile_id=:profile WHERE id=:id"),
                              {"profile": credential_proxy, "id": source.credential_id})
    await Catalog(pg_db, vault).update_provider(source.provider_id, 1, ProviderConfig(
        name="Synthetic provider", adapter="openai_compatible", root="https://provider.invalid/v1",
        proxy_profile_id=provider_proxy))
    context = await sources.context(source.id)
    assert context.effective_proxy_id == credential_proxy
    assert context.stamp.proxy_id == credential_proxy
    assert context.stamp.proxy_version == 1
    assert context.route.proxy_profile_id == credential_proxy
    async with pg_db.sessions.begin() as session:
        await session.execute(text("DELETE FROM proxy_profiles WHERE id=:id"), {"id": credential_proxy})
    from autobuild_json.gateway.errors import GatewayError
    with pytest.raises(GatewayError, match="invalid_state"):
        await sources.context(source.id)


async def test_no_auth_direct_context_never_opens_credential_cipher(pg_db):
    from autobuild_json.gateway.catalog.sources import SourceRepository
    from autobuild_json.gateway.providers.catalog import Catalog
    from autobuild_json.gateway.routing.records import ProviderConfig
    from autobuild_json.gateway.secrets import Vault
    from tests.gateway.catalog_support import source_case, synthetic_vault

    _, source = await source_case(pg_db)
    await Catalog(pg_db, synthetic_vault()).update_provider(source.provider_id, 1, ProviderConfig(
        name="No auth", adapter="openai_compatible", root="https://provider.invalid/v1", auth_mode="none"))
    wrong_vault = Vault({"v2": b"b" * 32}, active="v2")
    context = await SourceRepository(pg_db, wrong_vault, b"s" * 32).context(source.id)
    assert context.route.auth_mode == "none"
    assert context.effective_proxy_id is None


async def test_proxy_reseal_and_label_preserve_content_digest_but_entry_change_does_not(pg_db):
    import json
    from sqlalchemy import text
    from tests.gateway.catalog_support import source_case, synthetic_vault

    sources, source = await source_case(pg_db)
    vault = synthetic_vault()
    profile = uuid4()
    config = {"mode": "fixed", "region": "random", "protocol": "http", "rotate": False}

    async def set_profile(raw, name, version):
        cipher = vault.seal("proxy_profile", profile, raw.encode()).to_dict()
        async with pg_db.sessions.begin() as session:
            if version == 1:
                await session.execute(text("INSERT INTO proxy_profiles(id,name,config,encrypted_entries) "
                                           "VALUES (:id,:name,CAST(:config AS jsonb),CAST(:cipher AS jsonb))"),
                                      {"id": profile, "name": name, "config": json.dumps(config),
                                       "cipher": json.dumps(cipher)})
                await session.execute(text("UPDATE credentials SET profile_id=:profile WHERE id=:id"),
                                      {"profile": profile, "id": source.credential_id})
            else:
                await session.execute(text("UPDATE proxy_profiles SET name=:name,encrypted_entries=CAST(:cipher AS jsonb),"
                                           "version=:version WHERE id=:id"),
                                      {"id": profile, "name": name, "cipher": json.dumps(cipher), "version": version})

    await set_profile("http://127.0.0.1:8080", "First label", 1)
    first = await sources.context(source.id)
    await set_profile("http://127.0.0.1:8080", "Renamed", 2)
    resealed = await sources.context(source.id)
    assert resealed.stamp.content_digest == first.stamp.content_digest
    assert resealed.stamp.proxy_version == 2
    await set_profile("http://127.0.0.1:9090", "Renamed", 3)
    changed = await sources.context(source.id)
    assert changed.stamp.content_digest != first.stamp.content_digest


async def test_skip_locked_context_releases_provider_lock_on_credential_conflict(pg_db):
    import asyncio
    from sqlalchemy import text
    from tests.gateway.catalog_support import source_case

    sources, source = await source_case(pg_db)
    async with pg_db.sessions.begin() as holder:
        await holder.execute(text("SELECT id FROM credentials WHERE id=:id FOR UPDATE"),
                             {"id": source.credential_id})
        async with pg_db.sessions.begin() as worker:
            skipped = await asyncio.wait_for(sources.lock_context(worker, source.id, skip_locked=True), 2)
            assert skipped is None
            async with pg_db.sessions.begin() as observer:
                row = await observer.scalar(text("SELECT id FROM providers WHERE id=:id FOR UPDATE NOWAIT"),
                                            {"id": source.provider_id})
                assert row == source.provider_id


async def test_source_can_be_disabled_after_credential_is_disabled(pg_db):
    from autobuild_json.gateway.catalog.records import SourceInput
    from autobuild_json.gateway.providers.catalog import Catalog
    from tests.gateway.catalog_support import source_case, synthetic_vault

    sources, source = await source_case(pg_db)
    await Catalog(pg_db, synthetic_vault()).set_credential_enabled(source.credential_id, False)
    stopped = await sources.update(
        source.id, source.version,
        SourceInput(provider_id=source.provider_id, credential_id=source.credential_id,
                    mode=source.mode, enabled=False),
        "test-admin",
    )
    assert stopped.enabled is False
    assert stopped.version == source.version + 1
    assert stopped.next_run_at is None


async def test_cannot_activate_unhealthy_source_with_schedule_off(pg_db):
    from autobuild_json.gateway.catalog.records import SourceInput
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.providers.catalog import Catalog
    from tests.gateway.catalog_support import source_case, synthetic_vault

    sources, source = await source_case(pg_db)
    catalog = Catalog(pg_db, synthetic_vault())
    await catalog.set_credential_enabled(source.credential_id, False)
    stopped = await sources.update(
        source.id, source.version,
        SourceInput(provider_id=source.provider_id, credential_id=source.credential_id,
                    mode=source.mode, enabled=False),
        "test-admin",
    )
    with pytest.raises(GatewayError, match="invalid_state"):
        await sources.update(
            source.id, stopped.version,
            SourceInput(provider_id=source.provider_id, credential_id=source.credential_id,
                        mode=source.mode, enabled=True, schedule_enabled=False),
            "test-admin",
        )


async def test_source_schedule_can_be_stopped_after_profile_disappears(pg_db):
    import json
    from sqlalchemy import text
    from autobuild_json.gateway.catalog.records import SourceInput
    from tests.gateway.catalog_support import source_case, synthetic_vault

    sources, source = await source_case(pg_db)
    profile_id = uuid4()
    vault = synthetic_vault()
    cipher = vault.seal("proxy_profile", profile_id, b"http://127.0.0.1:8080").to_dict()
    async with pg_db.sessions.begin() as session:
        await session.execute(text("INSERT INTO proxy_profiles(id,name,config,encrypted_entries) "
                                   "VALUES (:id,'synthetic',CAST(:config AS jsonb),CAST(:cipher AS jsonb))"),
                              {"id": profile_id,
                               "config": json.dumps({"mode": "fixed", "region": "random",
                                                     "protocol": "http", "rotate": False}),
                               "cipher": json.dumps(cipher)})
        await session.execute(text("UPDATE credentials SET profile_id=:profile WHERE id=:credential"),
                              {"profile": profile_id, "credential": source.credential_id})
    scheduled = await sources.update(
        source.id, source.version,
        SourceInput(provider_id=source.provider_id, credential_id=source.credential_id,
                    mode=source.mode, schedule_enabled=True),
        "test-admin",
    )
    assert scheduled.next_run_at is not None
    async with pg_db.sessions.begin() as session:
        await session.execute(text("DELETE FROM proxy_profiles WHERE id=:id"), {"id": profile_id})
    stopped = await sources.update(
        source.id, scheduled.version,
        SourceInput(provider_id=source.provider_id, credential_id=source.credential_id,
                    mode=source.mode, schedule_enabled=False),
        "test-admin",
    )
    assert stopped.enabled is True
    assert stopped.schedule_enabled is False
    assert stopped.next_run_at is None
