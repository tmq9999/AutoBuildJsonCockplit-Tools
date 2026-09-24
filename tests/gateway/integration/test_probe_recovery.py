import pytest
import asyncio
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import text

from tests.gateway.catalog_support import probe_case

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_worker_recovery_fences_expired_probe_and_keeps_unknown_hold(pg_db):
    from uuid import uuid4
    from autobuild_json.gateway.catalog.worker import CatalogWorker

    async with probe_case(pg_db) as case:
        operation = await case.ops.enqueue(case.command, "admin")
        claim = await case.ops.claim(operation.id, uuid4())
        await case.accounting.reserve(claim, case.quote)
        await case.accounting.mark_dispatched(claim)
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE provider_operations SET deadline=clock_timestamp()-interval '16 seconds' WHERE id=:id"),
                                  {"id": operation.id})
        worker = CatalogWorker(pg_db, case.runner, None, case.runner.probe)
        assert await worker.repair_expired() == 1
        view = await case.ops.get(operation.id)
        assert view.state == "usage_pending" and view.cost is None
        async with pg_db.sessions() as session:
            row = (await session.execute(text("SELECT generation,owner FROM provider_operations WHERE id=:id"),
                                         {"id": operation.id})).one()
            assert row.generation > claim.generation and row.owner != claim.owner


@pytest.mark.parametrize("seam,state,cost", [("before_reserve", "failed", Decimal(0)),
    ("reserved", "failed", Decimal(0)), ("dispatched", "usage_pending", None),
    ("rejected", "failed", Decimal(0)), ("evidence", "succeeded", Decimal("0.000019")),
    ("money_settled", "succeeded", Decimal("0.000019"))])
async def test_two_workers_repair_each_crash_seam_once_after_grace(pg_db, seam, state, cost):
    from autobuild_json.gateway.catalog.worker import CatalogWorker
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.metering.records import Usage
    async with probe_case(pg_db) as case:
        operation = await case.ops.enqueue(case.command, "admin")
        claim = await case.ops.claim(operation.id, uuid4())
        if seam != "before_reserve":
            attempt = await case.accounting.reserve(claim, case.quote)
        if seam not in {"before_reserve", "reserved"}:
            await case.accounting.mark_dispatched(claim)
        if seam in {"evidence", "money_settled"}:
            await case.accounting.record_evidence(claim, Usage(4, 5), "success")
        elif seam == "rejected":
            await case.accounting.record_evidence(claim, None, "rejected")
        if seam == "money_settled":
            await case.services.budgets.settle(attempt, Decimal("0.000019"))
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE provider_operations SET deadline=clock_timestamp()-interval '10 seconds' WHERE id=:id"), {"id": operation.id})
        with pytest.raises(GatewayError, match="claim_lost"):
            await case.runner.probe.recover(operation.id)
        async with pg_db.sessions.begin() as session:
            await session.execute(text("UPDATE provider_operations SET deadline=clock_timestamp()-interval '16 seconds' WHERE id=:id"), {"id": operation.id})
        workers = [CatalogWorker(pg_db, case.runner, None, case.runner.probe) for _ in range(2)]
        await asyncio.gather(*(worker.repair_expired() for worker in workers))
        view = await case.ops.get(operation.id)
        assert (view.state, view.cost) == (state, cost)
        assert case.generation_calls == 0
        with pytest.raises(GatewayError, match="claim_lost"):
            await case.accounting.record_evidence(claim, Usage(1, 1), "success")
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='catalog.probe.settled' AND record_id=:id"), {"id": operation.id}) == 1
            assert await session.scalar(text("SELECT held FROM upstream_budgets")) == (Decimal("0.001048") if state == "usage_pending" else 0)
            assert await session.scalar(text("SELECT spent FROM upstream_budgets")) == (cost or 0)


async def test_recovery_and_reconcile_serialize_without_double_settlement(pg_db):
    async with probe_case(pg_db) as case:
        operation = await case.ops.enqueue(case.command, "admin")
        claim = await case.ops.claim(operation.id, uuid4())
        await case.accounting.reserve(claim, case.quote)
        await case.accounting.mark_dispatched(claim)
        pending = await case.accounting.settle(operation.id, claim=claim)
        await asyncio.gather(case.runner.probe.recover(operation.id),
            case.accounting.reconcile(operation.id, pending.version, Decimal("0.2"), "USD", "Invoice verified", "admin"))
        view = await case.ops.get(operation.id)
        assert view.state == "failed" and view.cost == Decimal("0.2")
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT spent FROM upstream_budgets")) == Decimal("0.2")
            assert await session.scalar(text("SELECT held FROM upstream_budgets")) == 0
            assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='catalog.probe.reconciled' AND record_id=:id"), {"id": operation.id}) == 1
