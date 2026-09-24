from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from autobuild_json.gateway.errors import GatewayError
from tests.gateway.catalog_support import snapshot_case, complete_snapshot, observe

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_retention_protects_references_and_purged_cursor_conflicts(pg_db):
    from autobuild_json.gateway.catalog.retention import Retention
    sources, source, store, first = await snapshot_case(pg_db, (observe("a"), observe("b")))
    cursor = (await store.page(source.id, limit=1)).next_cursor
    second = await complete_snapshot(store, sources, source.id, (observe("b"),))
    decision = await complete_snapshot(store, sources, source.id, (observe("c"),))
    publication = await complete_snapshot(store, sources, source.id, (observe("d"),))
    probe = await complete_snapshot(store, sources, source.id, (observe("e"),))
    previous = await complete_snapshot(store, sources, source.id, (observe("f"),))
    latest = await complete_snapshot(store, sources, source.id, (observe("g"),))
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE provider_operations SET created_at=clock_timestamp()-interval '31 days'"))
        await session.execute(text("UPDATE provider_operations SET kind='probe' WHERE id=:id"), {"id": probe})
        await session.execute(text("INSERT INTO catalog_decisions(source_id,upstream_id,published_run_id) VALUES (:source,'c',:run)"), {"source": source.id, "run": decision})
        await session.execute(text("INSERT INTO catalog_publications(id,source_id,run_id,payload_digest,result) VALUES (:run,:source,:run,'abc','{}')"), {"source": source.id, "run": publication})
    result = await Retention(pg_db).purge(datetime.now(timezone.utc))
    assert result["deleted"] == 2
    assert result["protected_over_limit"][str(source.id)] == 5
    async with pg_db.sessions() as session:
        retained = set((await session.execute(text("SELECT id FROM provider_operations"))).scalars())
        assert retained == {decision, publication, probe, previous, latest}
        assert await session.scalar(text("SELECT count(*) FROM provider_operation_claims WHERE operation_id IN (:first,:second)"), {"first": first, "second": second}) == 0
        assert await session.scalar(text("SELECT count(*) FROM catalog_entries WHERE run_id IN (:first,:second)"), {"first": first, "second": second}) == 0
    with pytest.raises(GatewayError, match="catalog_snapshot_stale") as exc:
        await store.page(source.id, cursor=cursor)
    assert exc.value.status == 409


async def test_retention_twenty_complete_and_batch_bound(pg_db):
    from autobuild_json.gateway.catalog.retention import Retention
    sources, source, store, first = await snapshot_case(pg_db, ())
    for _ in range(22):
        await complete_snapshot(store, sources, source.id, ())
    retention = Retention(pg_db)
    assert (await retention.purge(datetime.now(timezone.utc), limit=2))["deleted"] == 2
    assert (await retention.purge(datetime.now(timezone.utc)))["deleted"] == 1
    assert (await retention.purge(datetime.now(timezone.utc)-timedelta(days=1)))["deleted"] == 0


async def test_protected_only_source_does_not_starve_purgeable_source(pg_db):
    from uuid import uuid4
    from autobuild_json.gateway.catalog.retention import Retention
    _, source_a, _, protected_run = await snapshot_case(pg_db, ())
    _, source_b, _, template = await snapshot_case(pg_db, ())
    async with pg_db.sessions.begin() as session:
        # Make the lower-ordered source protected-only and add one old,
        # unreferenced operation to the other source.
        ordered = sorted((source_a.id, source_b.id))
        protected_source = ordered[0]
        purge_source = ordered[1]
        await session.execute(text("UPDATE provider_operations SET created_at=clock_timestamp()-interval '31 days'"))
        source_template = template if purge_source == source_b.id else protected_run
        old = uuid4()
        await session.execute(text("""INSERT INTO provider_operations
            (id,source_id,kind,state,version,payload_digest,input,expected,queued_expires_at,created_at,finished_at,result)
            SELECT :id,source_id,kind,state,version,payload_digest,input,expected,queued_expires_at,
                   clock_timestamp()-interval '31 days',clock_timestamp()-interval '31 days',result
            FROM provider_operations WHERE id=:template"""), {"id": old, "template": source_template})
    result = await Retention(pg_db).purge(datetime.now(timezone.utc), limit=1)
    assert result["deleted"] == 1
    assert result["protected_over_limit"][str(protected_source)] == 1
    assert len(result["protected_over_limit"]) <= 100
    async with pg_db.sessions() as session:
        assert not await session.scalar(text("SELECT EXISTS(SELECT 1 FROM provider_operations WHERE id=:id)"), {"id": old})
