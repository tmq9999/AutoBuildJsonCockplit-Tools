import pytest
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
