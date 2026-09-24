import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_ignore_and_undo_persist_across_snapshot_absence(pg_db):
    from tests.gateway.catalog_support import complete_snapshot, observe, snapshot_case
    from autobuild_json.gateway.catalog.decisions import DecisionStore
    from autobuild_json.gateway.catalog.records import DecisionEdit

    sources, source, store, _ = await snapshot_case(pg_db, (observe("a"),))
    decisions = DecisionStore(pg_db, sources)
    applied = await decisions.apply(source.id, source.version, (DecisionEdit(upstream_id="a", ignored=True),), "admin")
    assert applied[0]["ignored"] is True and applied[0]["version"] == 1
    source = await sources.get(source.id)
    await complete_snapshot(store, sources, source.id, ())
    source = await sources.get(source.id)
    await complete_snapshot(store, sources, source.id, (observe("a"),))
    page = await store.page(source.id, limit=50)
    assert page.items[0].decision["ignored"] is True
    source = await sources.get(source.id)
    applied = await decisions.apply(source.id, source.version, (DecisionEdit(upstream_id="a", ignored=False, expected_version=1),), "admin")
    assert applied[0]["ignored"] is False and applied[0]["version"] == 2
