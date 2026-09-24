"""Synthetic catalog fixtures shared by PostgreSQL integration tests."""

from autobuild_json.gateway.catalog.records import SourceInput
from autobuild_json.gateway.providers.catalog import Catalog
from autobuild_json.gateway.routing.records import ProviderConfig
from autobuild_json.gateway.secrets import Vault
from uuid import uuid4


def synthetic_vault():
    return Vault({"v1": b"a" * 32, "client_keys": b"p" * 32}, active="v1")


async def source_case(db, *, mode="openai_single"):
    from autobuild_json.gateway.catalog.sources import SourceRepository

    vault = synthetic_vault()
    catalog = Catalog(db, vault)
    adapter = {
        "openai_single": "openai_compatible",
        "openai_cursor": "openai_compatible",
        "anthropic": "anthropic",
        "gemini": "gemini",
        "ollama": "ollama",
    }[mode]
    provider_id = await catalog.create_provider(
        ProviderConfig(name="Synthetic provider", adapter=adapter, root="https://provider.invalid/v1")
    )
    credential_id = await catalog.put_credential(provider_id, "synthetic-upstream-secret")
    sources = SourceRepository(db, vault, b"s" * 32)
    source = await sources.create(
        SourceInput(provider_id=provider_id, credential_id=credential_id, mode=mode), "test-admin"
    )
    return sources, source


def observe(upstream_id):
    from autobuild_json.gateway.catalog.records import CatalogEntry
    return CatalogEntry(upstream_id=upstream_id, metadata={})


async def snapshot_case(db, entries):
    from autobuild_json.gateway.catalog.operations import OperationStore
    from autobuild_json.gateway.catalog.records import OperationRequest
    from autobuild_json.gateway.catalog.snapshots import SnapshotStore

    sources, source = await source_case(db)
    vault = synthetic_vault()
    ops = OperationStore(db, sources)
    store = SnapshotStore(db, sources, ops, vault.derive_key("client_keys", "catalog-cursor-v1"))
    expected = (await sources.context(source.id)).stamp
    request = OperationRequest(id=uuid4(), source_id=source.id, kind="discover", expected=expected)
    operation = await ops.enqueue(request, "test-admin")
    claim = await ops.claim(operation.id, uuid4())
    assert claim is not None
    await store.commit(claim, await sources.context(source.id), tuple(entries))
    return sources, source, store, operation.id


async def complete_snapshot(store, sources, source_id, entries):
    from autobuild_json.gateway.catalog.operations import OperationStore
    from autobuild_json.gateway.catalog.records import OperationRequest

    ops = store.ops if hasattr(store, "ops") else OperationStore(store.db, sources)
    expected = (await sources.context(source_id)).stamp
    request = OperationRequest(id=uuid4(), source_id=source_id, kind="discover", expected=expected)
    operation = await ops.enqueue(request, "test-admin")
    claim = await ops.claim(operation.id, uuid4())
    assert claim is not None
    await store.commit(claim, await sources.context(source_id), tuple(entries))
    return operation.id
