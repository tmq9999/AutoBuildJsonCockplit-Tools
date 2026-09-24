"""Publication is one compare-and-set transaction over reviewed mappings."""

from uuid import uuid4
import asyncio
import os
import subprocess
import sys

import pytest
from sqlalchemy import text

from autobuild_json.gateway.catalog.records import PublicationRequest, PublishItem
from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.routing.records import BindingConfig, ModelConfig, ProviderConfig
from tests.gateway.catalog_support import observe, snapshot_case

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_receipt_and_model_namespace_do_not_deadlock(pg_db, monkeypatch):
    """Two receipts held before either reaches the other's literal model name."""
    first, _, _, select_a, request_a = await publication_case(pg_db, ("a",))
    second, _, _, select_b, request_b = await publication_case(pg_db, ("b",))
    id_a, id_b = uuid4(), uuid4()
    a = request_a(select_a("a", f"publication:{id_b}")).model_copy(update={"id": id_a})
    b = request_b(select_b("b", f"publication:{id_a}")).model_copy(update={"id": id_b})
    arrived = set()
    both = asyncio.Event()
    # Gate immediately after receipt lock by intercepting source context read;
    # all real SQL remains active. Two distinct source dependencies avoid other locks.
    async def gate(repository, session, identity, **kwargs):
        arrived.add(identity)
        if len(arrived) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 2)
        return await originals[repository](session, identity, **kwargs)
    originals = {first.sources: first.sources.lock_context, second.sources: second.sources.lock_context}
    for repository in originals:
        async def wrapped(session, identity, _repository=repository, **kwargs):
            return await gate(_repository, session, identity, **kwargs)
        monkeypatch.setattr(repository, "lock_context", wrapped)
    results = await asyncio.wait_for(asyncio.gather(first.publish(a, "admin"), second.publish(b, "admin"), return_exceptions=True), 5)
    assert not any(isinstance(result, BaseException) for result in results), results
    assert {result.operation_id for result in results} == {id_a, id_b}


async def test_model_lock_remains_compatible_with_historical_key(pg_db):
    import hashlib
    from autobuild_json.gateway.providers.registry_writes import lock_model_names
    key = int.from_bytes(hashlib.sha256(b"historical-model").digest()[:8], "big", signed=True)
    async with pg_db.sessions.begin() as owner:
        await lock_model_names(owner, ("historical-model",))
        async with pg_db.sessions.begin() as contender:
            assert not await contender.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})


async def publication_case(db, upstream=("a", "b")):
    from autobuild_json.gateway.catalog.publish import Publisher

    sources, source, _, run_id = await snapshot_case(db, tuple(observe(item) for item in upstream))
    context = await sources.context(source.id)
    publisher = Publisher(db, sources)

    def selected(upstream_id, model_id=None, *, enabled=True, capabilities=frozenset({"text"})):
        model_id = model_id or upstream_id
        return PublishItem(upstream_id=upstream_id, public_model_id=model_id, identity=model_id,
                           input_bound=4096, output_bound=1024, capabilities=capabilities,
                           binding_enabled=enabled,
                           new_model=ModelConfig(model_id=model_id, identity=model_id, enabled=enabled))

    def request(*items):
        return PublicationRequest(id=uuid4(), source_id=source.id, run_id=run_id,
                                  expected=context.stamp, selections=tuple(items))

    return publisher, source, run_id, selected, request


async def test_publish_conflict_cannot_leave_half_a_batch(pg_db):
    from autobuild_json.gateway.providers.catalog import Catalog
    from tests.gateway.catalog_support import synthetic_vault

    publisher, _, _, selected, request = await publication_case(pg_db)
    await Catalog(pg_db, synthetic_vault()).put_model(ModelConfig(model_id="occupied", identity="occupied"))
    with pytest.raises(GatewayError, match="catalog_conflict"):
        await publisher.publish(request(selected("a", "fresh"), selected("b", "occupied")), "admin")
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM public_models WHERE id='fresh'")) == 0
        assert await session.scalar(text("SELECT count(*) FROM catalog_publications")) == 0


async def test_publish_replay_is_exact_and_does_not_duplicate_writes(pg_db):
    publisher, _, _, selected, request = await publication_case(pg_db)
    first = request(selected("a"))
    result = await publisher.publish(first, "admin")
    assert result.bindings[0].model_version == 1
    assert await publisher.publish(first, "admin") == result
    with pytest.raises(GatewayError, match="catalog_conflict"):
        await publisher.publish(first.model_copy(update={"selections": (selected("b"),)}), "admin")
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM model_bindings")) == 1
        assert await session.scalar(text("SELECT count(*) FROM catalog_publications")) == 1
        assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='catalog.published'")) == 1


async def test_publish_rejects_stale_run_and_unknown_capability(pg_db):
    publisher, source, run_id, selected, request = await publication_case(pg_db)
    with pytest.raises(GatewayError, match="unsupported_feature"):
        await publisher.publish(request(selected("a", capabilities=frozenset({"text", "audio"}))), "admin")
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE provider_operations SET finished_at=clock_timestamp()-interval '25 hours' WHERE id=:id"), {"id": run_id})
    with pytest.raises(GatewayError, match="catalog_snapshot_stale"):
        await publisher.publish(request(selected("a")), "admin")


async def test_publish_existing_model_preserves_coefficients_and_enabled(pg_db):
    from autobuild_json.gateway.providers.catalog import Catalog
    from tests.gateway.catalog_support import synthetic_vault

    publisher, _, _, selected, request = await publication_case(pg_db)
    model = ModelConfig(model_id="existing", identity="existing", enabled=False,
                        input_micro=1234567, output_micro=7654321)
    await Catalog(pg_db, synthetic_vault()).put_model(model)
    async with pg_db.sessions() as session:
        exact_before = await session.scalar(text("SELECT config::text FROM public_models WHERE id='existing'"))
    item = selected("a", "existing").model_copy(update={"new_model": None, "expected_model_version": 1})
    await publisher.publish(request(item), "admin")
    async with pg_db.sessions() as session:
        row = (await session.execute(text("SELECT config,version FROM public_models WHERE id='existing'"))).mappings().one()
        assert row["config"] == model.model_dump(mode="json")
        assert row["version"] == 1
        assert await session.scalar(text("SELECT config::text FROM public_models WHERE id='existing'")) == exact_before


async def test_publish_respects_new_decision_after_review(pg_db):
    from autobuild_json.gateway.catalog.decisions import DecisionStore
    from autobuild_json.gateway.catalog.records import DecisionEdit

    publisher, source, _, selected, request = await publication_case(pg_db)
    await DecisionStore(pg_db, publisher.sources).apply(source.id, source.version,
        (DecisionEdit(upstream_id="a", ignored=True),), "admin")
    with pytest.raises(GatewayError, match="catalog_conflict"):
        await publisher.publish(request(selected("a")), "admin")
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM public_models")) == 0
        assert await session.scalar(text("SELECT count(*) FROM model_bindings")) == 0
        assert await session.scalar(text("SELECT count(*) FROM catalog_publications")) == 0
        assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='catalog.published'")) == 0
        assert await session.scalar(text("SELECT ignored FROM catalog_decisions WHERE upstream_id='a'")) is True


async def test_publish_after_cosmetic_source_edit_with_current_stamp(pg_db):
    publisher, source, run_id, selected, request = await publication_case(pg_db)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE catalog_sources SET schedule_enabled=true,version=version+1 WHERE id=:id"), {"id": source.id})
    current = await publisher.sources.context(source.id)
    refreshed = request(selected("a")).model_copy(update={"expected": current.stamp})
    result = await publisher.publish(refreshed, "admin")
    assert result.run_id == run_id


async def test_publish_proxied_snapshot_parses_json_stamp(pg_db):
    from autobuild_json.gateway.admin.schemas import ProxyInput
    from autobuild_json.gateway.catalog.operations import OperationStore
    from autobuild_json.gateway.catalog.snapshots import SnapshotStore
    from autobuild_json.gateway.catalog.records import OperationRequest
    from autobuild_json.gateway.proxy.profiles import ProfileStore
    from tests.gateway.catalog_support import source_case, synthetic_vault

    sources, source = await source_case(pg_db)
    vault = synthetic_vault()
    profile = await ProfileStore(pg_db, vault, b"p" * 32).create(ProxyInput(
        name="Synthetic", mode="fixed", entries_text="http://93.184.216.34:39008"))
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE credentials SET profile_id=:profile WHERE id=:id"),
                              {"profile": profile, "id": source.credential_id})
    context = await sources.context(source.id)
    ops = OperationStore(pg_db, sources)
    store = SnapshotStore(pg_db, sources, ops, vault.derive_key("client_keys", "catalog-cursor-v1"))
    job = await ops.enqueue(OperationRequest(id=uuid4(), source_id=source.id, kind="discover", expected=context.stamp), "admin")
    claim = await ops.claim(job.id, uuid4())
    await store.commit(claim, context, (observe("a"),))
    publisher = __import__("autobuild_json.gateway.catalog.publish", fromlist=["Publisher"]).Publisher(pg_db, sources)
    item = PublishItem(upstream_id="a", public_model_id="a", identity="a", input_bound=10, output_bound=10,
                       capabilities=frozenset({"text"}), new_model=ModelConfig(model_id="a", identity="a"))
    result = await publisher.publish(PublicationRequest(id=uuid4(), source_id=source.id, run_id=job.id,
                                                        expected=context.stamp, selections=(item,)), "admin")
    assert result.run_id == job.id


async def test_publish_rejects_chat_continuation(pg_db):
    publisher, _, _, selected, request = await publication_case(pg_db)
    with pytest.raises(GatewayError, match="unsupported_feature"):
        await publisher.publish(request(selected("a", capabilities=frozenset({"text", "continuation"}))), "admin")


async def test_publication_digest_is_stable_across_hash_seeds():
    code = """from autobuild_json.gateway.catalog.publish import Publisher
from autobuild_json.gateway.catalog.records import PublicationRequest, PublishItem, ConfigStamp
from uuid import UUID
from autobuild_json.gateway.routing.records import ModelConfig
s=ConfigStamp(source_version=1,provider_version=1,credential_version=1,content_digest='a'*64)
i=PublishItem(upstream_id='a',public_model_id='a',identity='a',input_bound=1,output_bound=1,capabilities=frozenset({'text','tools','continuation'}),new_model=ModelConfig(model_id='a',identity='a'))
r=PublicationRequest(id=UUID(int=1),source_id=UUID(int=2),run_id=UUID(int=3),expected=s,selections=(i,))
print(Publisher._digest(r).hex())"""
    values = []
    for seed in ("1", "2", "3"):
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                env={**os.environ, "PYTHONHASHSEED": seed}, check=True)
        values.append(result.stdout.strip())
    assert len(set(values)) == 1


async def test_manual_binding_waits_for_provider_before_model_name(pg_db):
    from autobuild_json.gateway.providers.catalog import Catalog
    from autobuild_json.gateway.providers.registry_writes import _lock_key
    from tests.gateway.catalog_support import synthetic_vault

    publisher, source, _, _, _ = await publication_case(pg_db)
    catalog = Catalog(pg_db, synthetic_vault())
    await catalog.put_model(ModelConfig(model_id="race", identity="race"))
    binding = BindingConfig(id=uuid4(), provider_id=source.provider_id, credential_id=source.credential_id,
                            public_model_id="race", upstream_model="a", identity="race",
                            input_bound=10, output_bound=10)
    async with pg_db.sessions.begin() as blocked:
        await blocked.execute(text("SELECT id FROM providers WHERE id=:id FOR UPDATE"), {"id": source.provider_id})
        task = asyncio.create_task(catalog.put_binding(binding, create_only=True))
        try:
            for _ in range(100):
                async with pg_db.sessions() as observer:
                    waiting = await observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' "
                        "AND query LIKE '%providers%' AND pid<>pg_backend_pid()"))
                if waiting:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("manual binding did not wait for provider row")
            async with pg_db.sessions.begin() as observer:
                available = await observer.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _lock_key("race")})
                assert available is True
        finally:
            await blocked.rollback()
            await asyncio.wait_for(task, 3)


async def test_publish_enables_all_models_without_expanding_specific_key(pg_db):
    from decimal import Decimal
    from autobuild_json.gateway.metering.costs import CostSchedule
    from autobuild_json.gateway.identity.policy import KeyPolicy
    from autobuild_json.gateway.identity.service import IdentityService
    from autobuild_json.gateway.providers.catalog import Catalog
    from tests.gateway.catalog_support import synthetic_vault

    publisher, source, _, selected, request = await publication_case(pg_db)
    identity = IdentityService(pg_db, b"p" * 32)
    owner = await identity.create_customer("Synthetic")
    all_key = await identity.create_key(owner, KeyPolicy(all_models=True, protocols=frozenset({"openai"})))
    specific_key = await identity.create_key(owner, KeyPolicy(model_ids=frozenset({"old"}), protocols=frozenset({"openai"})))
    all_principal = await identity.authenticate(all_key.secret, "openai")
    specific_principal = await identity.authenticate(specific_key.secret, "openai")
    catalog = Catalog(pg_db, synthetic_vault())
    async with pg_db.sessions() as session:
        stored = await session.scalar(text("SELECT config FROM providers WHERE id=:id"), {"id": source.provider_id})
    priced = ProviderConfig.model_validate(stored).model_copy(update={"cost_schedule": CostSchedule(
        "USD", Decimal("1.234567"), Decimal("9.876543"))})
    await catalog.update_provider(source.provider_id, 1, priced)
    current = await publisher.sources.context(source.id)
    async with pg_db.sessions() as session:
        before = (await session.execute(text("SELECT id,policy::text FROM api_keys ORDER BY id"))).all()
        provider_before = await session.scalar(text("SELECT config::text FROM providers WHERE id=:id"), {"id": source.provider_id})
    await publisher.publish(request(selected("a", "new")).model_copy(update={"expected": current.stamp}), "admin")
    assert [model.model_id for model in await catalog.list_models(all_principal)] == ["new"]
    assert [model.model_id for model in await catalog.list_models(specific_principal)] == []
    async with pg_db.sessions() as session:
        assert (await session.execute(text("SELECT id,policy::text FROM api_keys ORDER BY id"))).all() == before
        assert await session.scalar(text("SELECT config::text FROM providers WHERE id=:id"), {"id": source.provider_id}) == provider_before


async def test_two_same_id_publishes_have_one_receipt(pg_db):
    publisher, _, _, selected, request = await publication_case(pg_db)
    command = request(selected("a"))
    first, second = await asyncio.gather(publisher.publish(command, "admin"), publisher.publish(command, "admin"))
    assert first == second
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM catalog_publications")) == 1
        assert await session.scalar(text("SELECT count(*) FROM model_bindings")) == 1


async def test_publish_and_manual_alias_contend_for_same_name(pg_db):
    from autobuild_json.gateway.providers.catalog import Catalog
    from autobuild_json.gateway.providers.registry_writes import lock_model_names
    from tests.gateway.catalog_support import synthetic_vault

    publisher, _, _, selected, request = await publication_case(pg_db)
    catalog = Catalog(pg_db, synthetic_vault())
    await catalog.put_model(ModelConfig(model_id="target", identity="target"))
    async with pg_db.sessions.begin() as gate:
        await lock_model_names(gate, ("race",))
        alias_task = asyncio.create_task(catalog.put_alias("race", "target"))
        publish_task = asyncio.create_task(publisher.publish(request(selected("a", "race")), "admin"))
        await asyncio.sleep(0)
    outcomes = await asyncio.gather(alias_task, publish_task, return_exceptions=True)
    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM public_models WHERE id='race'")) + await session.scalar(
            text("SELECT count(*) FROM model_aliases WHERE alias='race'")) == 1


async def test_publish_rejects_nonlatest_snapshot(pg_db):
    from autobuild_json.gateway.catalog.operations import OperationStore
    from autobuild_json.gateway.catalog.snapshots import SnapshotStore
    from tests.gateway.catalog_support import complete_snapshot

    publisher, source, _, selected, request = await publication_case(pg_db)
    store = SnapshotStore(pg_db, publisher.sources, OperationStore(pg_db, publisher.sources), b"z" * 32)
    await complete_snapshot(store, publisher.sources, source.id, (observe("a"),))
    with pytest.raises(GatewayError, match="catalog_snapshot_stale"):
        await publisher.publish(request(selected("a")), "admin")


async def test_manual_binding_edit_invalidates_publish_version(pg_db):
    from autobuild_json.gateway.providers.catalog import Catalog
    from tests.gateway.catalog_support import synthetic_vault

    publisher, source, _, selected, request = await publication_case(pg_db)
    first = await publisher.publish(request(selected("a", "public")), "admin")
    binding_id = first.bindings[0].binding_id
    catalog = Catalog(pg_db, synthetic_vault())
    changed = BindingConfig(id=binding_id, provider_id=source.provider_id, credential_id=source.credential_id,
                            public_model_id="public", upstream_model="manual", identity="public",
                            input_bound=10, output_bound=10, enabled=False)
    await catalog.put_binding(changed, expected_version=1)
    second = selected("b", "public").model_copy(update={"new_model": None, "expected_model_version": 1,
        "binding_id": binding_id, "binding_version": 1})
    with pytest.raises(GatewayError, match="version_conflict"):
        await publisher.publish(request(second), "admin")
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM catalog_publications")) == 1
        assert await session.scalar(text("SELECT config->>'upstream_model' FROM model_bindings WHERE id=:id"),
                                    {"id": binding_id}) == "manual"


@pytest.mark.parametrize("writer_kind", ["source_update", "publish"])
async def test_scheduler_manual_publish_lock_order_completes_under_event_gate(pg_db, monkeypatch, writer_kind):
    from autobuild_json.gateway.catalog.operations import OperationStore
    from autobuild_json.gateway.catalog.records import SourceInput
    from autobuild_json.gateway.catalog.scheduler import Scheduler

    publisher, source, run_id, selected, request = await publication_case(pg_db, upstream=("gated",))
    sources = publisher.sources
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE catalog_sources SET schedule_enabled=true,next_run_at=clock_timestamp()-interval '1 second' WHERE id=:id"), {"id": source.id})
        # This test isolates due-source locking, not the separate terminal
        # acknowledgement path which uses blocking locks.
        await session.execute(text("UPDATE provider_operations SET schedule_completed_at=clock_timestamp() WHERE id=:id"), {"id": run_id})
    scheduler = Scheduler(pg_db, OperationStore(pg_db, sources), sources)
    entered, release = asyncio.Event(), asyncio.Event()
    original = sources._dependencies
    writer_task = None
    async def gated(session, provider_id, credential_id, **kwargs):
        result = await original(session, provider_id, credential_id, **kwargs)
        if asyncio.current_task() is writer_task:
            # Real CRUD has acquired provider/credential/profile locks and is
            # paused immediately before taking the source lock.
            entered.set()
            await release.wait()
        return result
    monkeypatch.setattr(sources, "_dependencies", gated)
    if writer_kind == "publish":
        writer_task = asyncio.create_task(publisher.publish(request(selected("gated", "gated")), "admin"))
    else:
        writer_task = asyncio.create_task(sources.update(source.id, source.version,
            SourceInput(provider_id=source.provider_id, credential_id=source.credential_id,
                        mode=source.mode, schedule_enabled=True), "admin"))
    scheduler_task = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        scheduler_task = asyncio.create_task(scheduler.tick())
        # SKIP LOCKED must complete while the writer still owns provider locks,
        # without queuing a job or delaying behind a reversed source->provider edge.
        assert await asyncio.wait_for(scheduler_task, 2) == []
        assert not writer_task.done()
        async with pg_db.sessions.begin() as session:
            await session.execute(text("SELECT id FROM catalog_sources WHERE id=:id FOR UPDATE NOWAIT"), {"id": source.id})
            assert await session.scalar(text("SELECT count(*) FROM provider_operations WHERE state='queued'")) == 0
        release.set()
        result = await asyncio.wait_for(writer_task, 2)
        if writer_kind == "publish":
            assert result.run_id == run_id
            assert result.bindings[0].public_model_id == "gated"
            async with pg_db.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM catalog_publications WHERE run_id=:id"), {"id": run_id}) == 1
        else:
            assert result.id == source.id and result.version == source.version + 1
    finally:
        release.set()
        tasks = [task for task in (writer_task, scheduler_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
