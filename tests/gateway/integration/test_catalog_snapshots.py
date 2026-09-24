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
