"""Synthetic catalog fixtures shared by PostgreSQL integration tests."""

from autobuild_json.gateway.catalog.records import SourceInput
from autobuild_json.gateway.providers.catalog import Catalog
from autobuild_json.gateway.routing.records import ProviderConfig
from autobuild_json.gateway.secrets import Vault


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
