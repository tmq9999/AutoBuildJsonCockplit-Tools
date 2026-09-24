"""Publication is one compare-and-set transaction over reviewed mappings."""

from uuid import uuid4

import pytest
from sqlalchemy import text

from autobuild_json.gateway.catalog.records import PublicationRequest, PublishItem
from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.routing.records import ModelConfig
from tests.gateway.catalog_support import observe, snapshot_case

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


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
    item = selected("a", "existing").model_copy(update={"new_model": None, "expected_model_version": 1})
    await publisher.publish(request(item), "admin")
    async with pg_db.sessions() as session:
        row = (await session.execute(text("SELECT config,version FROM public_models WHERE id='existing'"))).mappings().one()
        assert row["config"] == model.model_dump(mode="json")
        assert row["version"] == 1
