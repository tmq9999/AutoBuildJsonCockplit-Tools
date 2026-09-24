"""Synthetic catalog fixtures shared by PostgreSQL integration tests."""

from autobuild_json.gateway.catalog.records import SourceInput
from autobuild_json.gateway.providers.catalog import Catalog
from autobuild_json.gateway.routing.records import ProviderConfig
from autobuild_json.gateway.secrets import Vault
from uuid import uuid4
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import time

import httpx


def synthetic_vault():
    return Vault({"v1": b"a" * 32, "client_keys": b"p" * 32}, active="v1")


@asynccontextmanager
async def discovery_case(db, upstream, *, mode="openai_single", selection=None, kiot=None):
    """Real source, lease and admission paths; only the remote HTTP peer is synthetic."""
    from types import SimpleNamespace
    from autobuild_json.gateway.engine import Engine
    from autobuild_json.gateway.providers.discovery.client import DiscoveryClient
    from autobuild_json.gateway.providers.limits import ProviderLimits
    from autobuild_json.gateway.proxy.config import ProxySelection
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.pg_leases import PgLeaseStore
    from autobuild_json.gateway.transport.egress import EgressPolicy
    from autobuild_json.gateway.transport.http import Transport

    sources, source = await source_case(db, mode=mode)
    context = await sources.context(source.id)
    paths = []

    async def respond(request):
        paths.append(request.url.path)
        result = upstream(request)
        if hasattr(result, "__await__"):
            result = await result
        return result

    async def resolver(host, port):
        return ["93.184.216.34"]

    trusted = tuple(entry.server for entry in (selection.runtime_entries if selection and selection.mode in {"fixed", "pool"} else ()))
    if selection and selection.mode == "kiotproxy":
        trusted = ("http://93.184.216.34:39008",)
    policy = EgressPolicy(resolver=resolver, trusted_proxy_origins=trusted)
    transport = Transport(policy, adapter=httpx.MockTransport(respond))
    vault = synthetic_vault()
    credential_calls = []
    async def credential(route):
        credential_calls.append(route.credential_id)
        return await Engine.credential(SimpleNamespace(db=db, vault=vault), route)

    client = DiscoveryClient(transport, ProviderLimits(db), credential)
    proxy_manager = ProxyManager(PgLeaseStore(db), kiot=kiot)
    end = datetime.now(timezone.utc) + timedelta(seconds=60)
    async def check_current():
        if (await sources.context(source.id)).stamp.content_digest != context.stamp.content_digest:
            from autobuild_json.gateway.errors import GatewayError
            raise GatewayError("version_conflict", 409)

    async with proxy_manager.acquire(selection or ProxySelection("direct"), uuid4(), end) as lease:
        yield SimpleNamespace(client=client, context=context, lease=lease,
                              check_current=check_current, deadline=time.monotonic()+60,
                              request_paths=paths, sources=sources, source=source,
                              credential_calls=credential_calls)


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
        ProviderConfig(name="Synthetic provider", adapter=adapter, root="https://provider.invalid/v1",
                       auth_mode="none" if mode == "ollama" else "bearer")
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
