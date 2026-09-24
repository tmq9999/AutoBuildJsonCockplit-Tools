import asyncio
import pytest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_coalesced_id_stays_claimed_after_completion(pg_db):
    from tests.gateway.catalog_support import source_case
    from autobuild_json.gateway.catalog.records import OperationRequest
    from autobuild_json.gateway.catalog.operations import OperationStore

    sources, source = await source_case(pg_db)
    ops = OperationStore(pg_db, sources)
    expected = (await sources.context(source.id)).stamp
    a = OperationRequest(id=uuid4(), source_id=source.id, kind="check", expected=expected)
    b = a.model_copy(update={"id": uuid4()})
    first, second = await ops.enqueue(a, "admin"), await ops.enqueue(b, "admin")
    assert first.id == second.id
    claim = await ops.claim(first.id, uuid4())
    await ops.finish(claim, "succeeded", result={"reachable": True})
    assert (await ops.enqueue(b, "admin")).id == first.id
    assert await ops.claim(first.id, uuid4()) is None


async def test_client_id_payload_mismatch_is_rejected_before_context_lookup(pg_db):
    from tests.gateway.catalog_support import source_case
    from autobuild_json.gateway.catalog.records import OperationRequest
    from autobuild_json.gateway.catalog.operations import OperationStore
    from autobuild_json.gateway.errors import GatewayError

    sources, source = await source_case(pg_db)
    ops = OperationStore(pg_db, sources)
    expected = (await sources.context(source.id)).stamp
    client_id = uuid4()
    await ops.enqueue(OperationRequest(id=client_id, source_id=source.id, kind="check", expected=expected), "admin")
    await sources.update(source.id, source.version,
                         source.model_copy(update={"enabled": False}), "admin")
    with pytest.raises(GatewayError) as caught:
        await ops.enqueue(OperationRequest(id=client_id, source_id=source.id, kind="discover", expected=expected), "admin")
    assert caught.value.code == "payload_mismatch"


async def test_cancel_queued_operation_is_idempotent_for_current_version(pg_db):
    from tests.gateway.catalog_support import source_case
    from autobuild_json.gateway.catalog.records import OperationRequest
    from autobuild_json.gateway.catalog.operations import OperationStore

    sources, source = await source_case(pg_db)
    ops = OperationStore(pg_db, sources)
    expected = (await sources.context(source.id)).stamp
    view = await ops.enqueue(OperationRequest(id=uuid4(), source_id=source.id, kind="check", expected=expected), "admin")
    cancelled = await ops.cancel(view.id, view.version, "admin")
    assert cancelled.state == "cancelled"
    assert (await ops.cancel(view.id, cancelled.version, "admin")).state == "cancelled"


async def test_running_cancel_blocks_terminal_write(pg_db):
    from tests.gateway.catalog_support import source_case
    from autobuild_json.gateway.catalog.records import OperationRequest
    from autobuild_json.gateway.catalog.operations import OperationStore
    from autobuild_json.gateway.errors import GatewayError

    sources, source = await source_case(pg_db)
    ops = OperationStore(pg_db, sources)
    expected = (await sources.context(source.id)).stamp
    view = await ops.enqueue(OperationRequest(id=uuid4(), source_id=source.id, kind="check", expected=expected), "admin")
    claim = await ops.claim(view.id, uuid4())
    running = await ops.get(view.id)
    cancelled = await ops.cancel(view.id, running.version, "admin")
    assert cancelled.cancel_requested
    with pytest.raises(GatewayError) as caught:
        await ops.finish(claim, "succeeded", result={"reachable": True})
    assert caught.value.code == "claim_lost"
    repeated = await ops.cancel(view.id, cancelled.version, "admin")
    assert repeated.version == cancelled.version


async def test_claim_stales_disabled_source_and_expired_queue(pg_db):
    from sqlalchemy import text
    from tests.gateway.catalog_support import source_case
    from autobuild_json.gateway.catalog.records import OperationRequest
    from autobuild_json.gateway.catalog.operations import OperationStore

    sources, source = await source_case(pg_db)
    ops = OperationStore(pg_db, sources)
    expected = (await sources.context(source.id)).stamp
    disabled = await ops.enqueue(OperationRequest(id=uuid4(), source_id=source.id, kind="check", expected=expected), "admin")
    await sources.update(source.id, source.version, source.model_copy(update={"enabled": False}), "admin")
    assert await ops.claim(disabled.id, uuid4()) is None
    assert (await ops.get(disabled.id)).state == "stale"

    sources, source = await source_case(pg_db)
    ops = OperationStore(pg_db, sources)
    expected = (await sources.context(source.id)).stamp
    expired = await ops.enqueue(OperationRequest(id=uuid4(), source_id=source.id, kind="check", expected=expected), "admin")
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE provider_operations SET queued_expires_at=:past WHERE id=:id"),
                              {"id": expired.id, "past": datetime.now(timezone.utc) - timedelta(seconds=1)})
    assert await ops.claim(expired.id, uuid4()) is None
    assert (await ops.get(expired.id)).state == "stale"


async def test_claim_stales_changed_context_and_active_payload_conflicts(pg_db):
    from sqlalchemy import text
    from tests.gateway.catalog_support import source_case
    from autobuild_json.gateway.catalog.records import OperationRequest
    from autobuild_json.gateway.catalog.operations import OperationStore
    from autobuild_json.gateway.errors import GatewayError

    sources, source = await source_case(pg_db)
    ops = OperationStore(pg_db, sources)
    expected = (await sources.context(source.id)).stamp
    first = await ops.enqueue(OperationRequest(id=uuid4(), source_id=source.id, kind="check", expected=expected), "admin")
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE providers SET version=version+1 WHERE id=:id"), {"id": source.provider_id})
    assert await ops.claim(first.id, uuid4()) is None
    assert (await ops.get(first.id)).state == "stale"

    sources, source = await source_case(pg_db)
    ops = OperationStore(pg_db, sources)
    expected = (await sources.context(source.id)).stamp
    active = OperationRequest(id=uuid4(), source_id=source.id, kind="check", expected=expected)
    await ops.enqueue(active, "admin")
    with pytest.raises(GatewayError) as caught:
        await ops.enqueue(active.model_copy(update={"id": uuid4(), "kind": "discover"}), "admin")
    assert caught.value.code == "operation_conflict"


async def test_same_client_concurrent_enqueue_coalesces(pg_db):
    from tests.gateway.catalog_support import source_case
    from autobuild_json.gateway.catalog.records import OperationRequest
    from autobuild_json.gateway.catalog.operations import OperationStore

    sources, source = await source_case(pg_db)
    ops = OperationStore(pg_db, sources)
    expected = (await sources.context(source.id)).stamp
    request = OperationRequest(id=uuid4(), source_id=source.id, kind="check", expected=expected)
    views = await asyncio.gather(*(ops.enqueue(request, "admin") for _ in range(2)))
    assert views[0].id == views[1].id == request.id


async def test_distinct_clients_concurrent_enqueue_coalesce_and_only_one_worker_claims(pg_db):
    from sqlalchemy import text
    from tests.gateway.catalog_support import source_case
    from autobuild_json.gateway.catalog.records import OperationRequest
    from autobuild_json.gateway.catalog.operations import OperationStore

    sources, source = await source_case(pg_db)
    ops = OperationStore(pg_db, sources)
    expected = (await sources.context(source.id)).stamp
    first = OperationRequest(id=uuid4(), source_id=source.id, kind="check", expected=expected)
    second = first.model_copy(update={"id": uuid4()})
    views = await asyncio.gather(ops.enqueue(first, "admin"), ops.enqueue(second, "admin"))
    assert views[0].id == views[1].id
    claims = await asyncio.gather(ops.claim(views[0].id, uuid4()), ops.claim(views[0].id, uuid4()))
    assert sum(claim is not None for claim in claims) == 1
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM provider_operations WHERE source_id=:id"),
                                    {"id": source.id}) == 1
        assert await session.scalar(text("SELECT count(*) FROM provider_operation_claims WHERE operation_id=:id"),
                                    {"id": views[0].id}) == 2


async def test_result_rejects_raw_or_unbounded_fields(pg_db):
    from tests.gateway.catalog_support import source_case
    from autobuild_json.gateway.catalog.records import OperationRequest
    from autobuild_json.gateway.catalog.operations import OperationStore
    from autobuild_json.gateway.errors import GatewayError

    sources, source = await source_case(pg_db)
    ops = OperationStore(pg_db, sources)
    expected = (await sources.context(source.id)).stamp
    view = await ops.enqueue(OperationRequest(id=uuid4(), source_id=source.id, kind="check", expected=expected), "admin")
    claim = await ops.claim(view.id, uuid4())
    with pytest.raises(GatewayError) as caught:
        await ops.finish(claim, "succeeded", result={"raw_body": "provider response"})
    assert caught.value.code == "invalid_request"


async def test_lost_generation_rejects_all_terminal_paths(pg_db):
    from sqlalchemy import text
    from tests.gateway.catalog_support import source_case
    from autobuild_json.gateway.catalog.records import OperationRequest
    from autobuild_json.gateway.catalog.operations import OperationStore
    from autobuild_json.gateway.errors import GatewayError

    sources, source = await source_case(pg_db)
    ops = OperationStore(pg_db, sources)
    expected = (await sources.context(source.id)).stamp
    view = await ops.enqueue(OperationRequest(id=uuid4(), source_id=source.id, kind="check", expected=expected), "admin")
    claim = await ops.claim(view.id, uuid4())
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE provider_operations SET generation=generation+1 WHERE id=:id"),
                              {"id": view.id})
    for state in ("succeeded", "failed", "cancelled", "stale", "usage_pending"):
        with pytest.raises(GatewayError) as caught:
            await ops.finish(claim, state)
        assert caught.value.code == "claim_lost"
    with pytest.raises(GatewayError):
        await ops.mark_dispatched(claim, uuid4())
    assert not await ops.heartbeat(claim)
    assert (await ops.get(view.id)).state == "running"
