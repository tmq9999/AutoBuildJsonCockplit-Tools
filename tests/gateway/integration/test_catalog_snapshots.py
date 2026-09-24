import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_cursor_does_not_jump_to_a_new_snapshot(pg_db):
    from tests.gateway.catalog_support import complete_snapshot, observe, snapshot_case

    sources, source, store, run_id = await snapshot_case(pg_db, (observe("a"), observe("b")))
    first = await store.page(source.id, limit=1)
    assert first.run_id == run_id and first.next_cursor
    await complete_snapshot(store, sources, source.id, (observe("z"),))
    second = await store.page(source.id, cursor=first.next_cursor, limit=1)
    assert second.run_id == run_id and second.items[0].upstream_id == "b"


async def test_snapshot_diff_marks_missing_and_unchanged(pg_db):
    from tests.gateway.catalog_support import complete_snapshot, observe, snapshot_case

    sources, source, store, _ = await snapshot_case(pg_db, (observe("a"), observe("b")))
    await complete_snapshot(store, sources, source.id, (observe("b"), observe("c")))
    page = await store.page(source.id, limit=200)
    assert [(item.upstream_id, item.diff) for item in page.items] == [("a", "missing"), ("b", "unchanged"), ("c", "added")]


async def test_snapshot_preserves_observed_datetime_metadata(pg_db):
    from datetime import datetime, timezone
    from tests.gateway.catalog_support import snapshot_case
    from autobuild_json.gateway.catalog.records import CatalogEntry, ObservedValue

    entry = CatalogEntry(upstream_id="meta", metadata={
        "owner": ObservedValue(value="Vendor", path="owned_by", observed_at=datetime(2026, 9, 24, tzinfo=timezone.utc))
    })
    _, source, store, _ = await snapshot_case(pg_db, (entry,))
    page = await store.page(source.id)
    assert page.items[0].entry.metadata["owner"].observed_at.year == 2026


async def test_snapshot_rejects_sensitive_metadata(pg_db):
    from tests.gateway.catalog_support import snapshot_case
    from autobuild_json.gateway.catalog.records import CatalogEntry, ObservedValue
    from autobuild_json.gateway.errors import GatewayError
    from datetime import datetime, timezone

    entry = CatalogEntry(upstream_id="secret", metadata={
        "api_key": ObservedValue(value="secret-value", observed_at=datetime(2026, 9, 24, tzinfo=timezone.utc))
    })
    with pytest.raises(GatewayError) as caught:
        await snapshot_case(pg_db, (entry,))
    assert caught.value.code == "invalid_request"


@pytest.mark.parametrize("payload", [
    {"outer": {"auth": "secret-value"}},
    {"outer": [{"api_key": "synthetic"}]},
    [{"safe": ["ok", "Bearer synthetic-token"]}],
    {"safe": {"nested": ["line\nbreak"]}},
])
async def test_snapshot_rejects_nested_sensitive_metadata(pg_db, payload):
    from datetime import datetime, timezone
    from autobuild_json.gateway.catalog.records import CatalogEntry, ObservedValue
    from autobuild_json.gateway.errors import GatewayError
    from tests.gateway.catalog_support import snapshot_case

    entry = CatalogEntry(upstream_id="nested", metadata={
        "info": ObservedValue(value=payload, observed_at=datetime(2026, 9, 24, tzinfo=timezone.utc))
    })
    with pytest.raises(GatewayError) as caught:
        await snapshot_case(pg_db, (entry,))
    assert caught.value.code == "invalid_request"


async def test_snapshot_keeps_safe_nested_metadata(pg_db):
    from datetime import datetime, timezone
    from autobuild_json.gateway.catalog.records import CatalogEntry, ObservedValue
    from tests.gateway.catalog_support import snapshot_case

    entry = CatalogEntry(upstream_id="nested", metadata={
        "capabilities": ObservedValue(value={"modalities": ["text", "image"], "limits": {"input": 1000}},
                                      path="capabilities", observed_at=datetime(2026, 9, 24, tzinfo=timezone.utc))
    })
    _, source, store, _ = await snapshot_case(pg_db, (entry,))
    assert (await store.page(source.id)).items[0].entry.metadata["capabilities"].value == {
        "modalities": ["text", "image"], "limits": {"input": 1000}}


async def test_cursor_for_purged_snapshot_is_stale(pg_db):
    from sqlalchemy import text
    from tests.gateway.catalog_support import snapshot_case
    from autobuild_json.gateway.errors import GatewayError
    from tests.gateway.catalog_support import observe

    _, source, store, run_id = await snapshot_case(pg_db, (observe("a"), observe("b")))
    page = await store.page(source.id, limit=1)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("DELETE FROM catalog_entries WHERE run_id=:run"), {"run": run_id})
    with pytest.raises(GatewayError) as caught:
        await store.page(source.id, cursor=page.next_cursor, limit=1)
    assert caught.value.code == "catalog_snapshot_stale"


async def test_cross_source_pointer_is_rejected(pg_db):
    from sqlalchemy import text
    from tests.gateway.catalog_support import snapshot_case, observe
    from autobuild_json.gateway.errors import GatewayError
    from tests.gateway.catalog_support import source_case

    _, source_a, store_a, run_id = await snapshot_case(pg_db, (observe("a"),))
    sources_b, source_b = await source_case(pg_db)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE catalog_sources SET latest_successful_run_id=:run WHERE id=:source"),
                              {"run": run_id, "source": source_b.id})
    with pytest.raises(GatewayError) as caught:
        await store_a.page(source_b.id)
    assert caught.value.code == "catalog_snapshot_stale"
