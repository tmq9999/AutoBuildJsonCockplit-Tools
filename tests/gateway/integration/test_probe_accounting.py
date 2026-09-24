import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from autobuild_json.gateway.catalog.operations import OperationStore
from autobuild_json.gateway.catalog.records import OperationRequest, ProbeIntent
from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.metering.budgets import BudgetService
from autobuild_json.gateway.metering.costs import CostSchedule
from autobuild_json.gateway.metering.records import Usage
from autobuild_json.gateway.providers.catalog import Catalog
from autobuild_json.gateway.routing.records import BindingConfig, ModelConfig
from tests.gateway.catalog_support import source_case, synthetic_vault

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def case(db, *, zero=False):
    from autobuild_json.gateway.catalog.probe_quote import ProbeQuotes
    from autobuild_json.gateway.catalog.probe_accounting import ProbeAccounting
    sources, source = await source_case(db)
    catalog = Catalog(db, synthetic_vault())
    budgets = BudgetService(db)
    budget = await budgets.create("USD", Decimal(10))
    provider = (await sources.context(source.id)).provider.model_copy(update={
        "budget_id": budget, "cost_schedule": CostSchedule("USD", Decimal(0 if zero else 1), Decimal(0 if zero else 3))})
    await catalog.update_provider(source.provider_id, 1, provider)
    await catalog.put_model(ModelConfig(model_id="probe-model", identity="probe"))
    binding = BindingConfig(uuid4(), source.provider_id, source.credential_id, "probe-model", "upstream", "probe", 1000, 16, enabled=False)
    await catalog.put_binding(binding)
    quotes = ProbeQuotes(db, sources)
    quote = await quotes.quote(source.id, binding.id)
    ops = OperationStore(db, sources)
    request = OperationRequest(id=uuid4(), source_id=source.id, kind="probe", expected=quote.stamp,
        probe=ProbeIntent(binding_id=binding.id, quote_digest=quote.digest, expected=quote.stamp,
                          max_hold=quote.upper_cost, currency="USD", acknowledged=True))
    job = await ops.enqueue(request, "synthetic-admin")
    claim = await ops.claim(job.id, uuid4())
    assert claim
    return SimpleNamespace(**locals(), accounting=ProbeAccounting(db, ops, budgets))


async def counts(db):
    async with db.sessions() as session:
        return [await session.scalar(text(f"SELECT count(*) FROM {table}")) for table in
                ("requests", "quota_buckets", "usage_ledger", "attempts")]


async def test_operation_view_reads_exact_persisted_money(pg_db):
    sources, source = await source_case(pg_db)
    ops = OperationStore(pg_db, sources)
    job = await ops.enqueue(OperationRequest(id=uuid4(), source_id=source.id, kind="check",
        expected=(await sources.context(source.id)).stamp), "admin")
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE provider_operations SET cost=' {\"amount\":\"100000000000000000001.000000000001\",\"currency\":\"USD\"}'::jsonb WHERE id=:id"), {"id": job.id})
    assert (await ops.get(job.id)).cost == Decimal("100000000000000000001.000000000001")


async def test_reserve_and_settle_are_exact_idempotent_and_customer_isolated(pg_db):
    c = await case(pg_db)
    before = await counts(pg_db)
    attempts = await asyncio.gather(*(c.accounting.reserve(c.claim, c.quote) for _ in range(2)))
    assert attempts[0] == attempts[1]
    assert await c.budgets.balance(c.budget) == (Decimal(0), Decimal("0.001048"), Decimal(10))
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT request_id FROM upstream_reservations")) is None
        assert await session.scalar(text("SELECT count(*) FROM upstream_reservations")) == 1
    await c.accounting.mark_dispatched(c.claim)
    with pytest.raises(GatewayError):
        await c.accounting.mark_dispatched(c.claim)
    await c.accounting.record_evidence(c.claim, Usage(10, 2), "success")
    views = await asyncio.gather(*(c.accounting.settle(c.job.id, claim=c.claim) for _ in range(2)))
    assert views[0].cost == views[1].cost == Decimal("0.000016")
    assert (await c.ops.get(c.job.id)).state == "succeeded"
    assert await c.budgets.balance(c.budget) == (Decimal("0.000016"), Decimal(0), Decimal(10))
    assert await counts(pg_db) == before


async def test_zero_explicit_price_still_reserves_and_settles(pg_db):
    c = await case(pg_db, zero=True)
    await c.accounting.reserve(c.claim, c.quote)
    await c.accounting.mark_dispatched(c.claim)
    await c.accounting.record_evidence(c.claim, Usage(1, 1), "success")
    assert (await c.accounting.settle(c.job.id, claim=c.claim)).cost == 0


@pytest.mark.parametrize("change", ["cost", "credential", "proxy", "binding", "budget"])
async def test_stale_quote_never_writes_money(pg_db, change):
    c = await case(pg_db)
    async with pg_db.sessions.begin() as session:
        if change == "cost":
            await session.execute(text("UPDATE providers SET config=jsonb_set(config,'{cost_schedule,input_per_million}','\"2\"') WHERE id=:id"), {"id": c.source.provider_id})
        elif change == "credential":
            await c.catalog.rotate_credential(c.source.credential_id, "synthetic-rotated")
        elif change == "proxy":
            await session.execute(text("UPDATE providers SET version=version+1 WHERE id=:id"), {"id": c.source.provider_id})
        elif change == "binding":
            await session.execute(text("UPDATE model_bindings SET version=version+1 WHERE id=:id"), {"id": c.binding.id})
        else:
            await session.execute(text("UPDATE upstream_budgets SET version=version+1 WHERE id=:id"), {"id": c.budget})
    with pytest.raises(GatewayError, match="version_conflict"):
        await c.accounting.reserve(c.claim, c.quote)
    assert await c.budgets.balance(c.budget) == (Decimal(0), Decimal(0), Decimal(10))


async def test_link_failure_rolls_back_reservation(pg_db):
    c = await case(pg_db)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("ALTER TABLE provider_operations ADD CONSTRAINT test_fail_link CHECK (attempt_id IS NULL)"))
    with pytest.raises(Exception):
        await c.accounting.reserve(c.claim, c.quote)
    assert await c.budgets.balance(c.budget) == (Decimal(0), Decimal(0), Decimal(10))
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM upstream_reservations")) == 0


async def test_incomplete_cache_cost_pending_and_money_only_reconcile(pg_db):
    c = await case(pg_db)
    await c.accounting.reserve(c.claim, c.quote)
    await c.accounting.mark_dispatched(c.claim)
    await c.accounting.record_evidence(c.claim, Usage(10, 2, cached_read=3), "success")
    pending = await c.accounting.settle(c.job.id, claim=c.claim)
    assert pending.state == "usage_pending" and pending.cost is None
    reason = "Verified provider invoice"
    outcomes = await asyncio.gather(*(c.accounting.reconcile(c.job.id, pending.version, Decimal("0.2"), "USD", reason, "admin") for _ in range(2)))
    assert outcomes[0].cost == outcomes[1].cost == Decimal("0.2")
    assert outcomes[0].usage == Usage(10, 2, cached_read=3)
    with pytest.raises(GatewayError, match="payload_mismatch"):
        await c.accounting.reconcile(c.job.id, pending.version, Decimal("0.3"), "USD", reason, "admin")
    async with pg_db.sessions() as session:
        audits = (await session.execute(text("SELECT action,details FROM audit_events WHERE record_id=:id"), {"id": c.job.id})).all()
        assert reason in str(audits)
        assert sum(a[0] == "catalog.probe.reconciled" for a in audits) == 1
        assert any(a[0] == "catalog.probe.cost_overrun" for a in audits)
        assert await session.scalar(text("SELECT settlement_source FROM provider_operations WHERE id=:id"), {"id": c.job.id}) == "admin_adjusted"


async def test_foreign_owner_cannot_record_or_settle_live_probe(pg_db):
    c = await case(pg_db)
    await c.accounting.reserve(c.claim, c.quote)
    await c.accounting.mark_dispatched(c.claim)
    for claim in (None, c.claim.model_copy(update={"owner": uuid4()})):
        with pytest.raises(GatewayError, match="claim_lost"):
            await c.accounting.settle(c.job.id, claim=claim)
    with pytest.raises(GatewayError, match="claim_lost"):
        await c.accounting.record_evidence(c.claim.model_copy(update={"owner": uuid4()}), Usage(1, 1), "success")


@pytest.mark.parametrize("change", ["foreign", "output", "input", "cost", "currency", "disabled", "codex"])
async def test_quote_rejects_unusable_config(pg_db, change):
    c = await case(pg_db)
    if change in {"output", "input", "foreign"}:
        binding = replace(c.binding, **({"output_bound": 15} if change == "output" else {"input_bound": 0} if change == "input" else {"credential_id": await c.catalog.put_credential(c.source.provider_id, "synthetic-other")}))
        await c.catalog.put_binding(binding)
    else:
        update = {"cost_schedule": None} if change == "cost" else {"cost_schedule": CostSchedule("EUR", Decimal(1), Decimal(1))} if change == "currency" else {"enabled": False} if change == "disabled" else {"adapter": "codex_oauth"}
        await c.catalog.update_provider(c.source.provider_id, 2, c.provider.model_copy(update=update))
    with pytest.raises(GatewayError):
        await c.quotes.quote(c.source.id, c.binding.id)


@pytest.mark.parametrize("dispatched,evidence,state,cost", [(False, None, "failed", Decimal(0)),
    (True, None, "usage_pending", None), (True, "rejected", "failed", Decimal(0)),
    (True, "success", "succeeded", Decimal("0.000016"))])
async def test_expired_recovery_uses_durable_evidence_and_fences_owner(pg_db, dispatched, evidence, state, cost):
    c = await case(pg_db)
    await c.accounting.reserve(c.claim, c.quote)
    if dispatched:
        await c.accounting.mark_dispatched(c.claim)
    if evidence:
        await c.accounting.record_evidence(c.claim, Usage(10, 2) if evidence == "success" else None, evidence)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE provider_operations SET deadline=clock_timestamp()-interval '16 seconds' WHERE id=:id"), {"id": c.job.id})
    view = await c.accounting.settle(c.job.id)
    assert (view.state, view.cost) == (state, cost)
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT generation FROM provider_operations WHERE id=:id"), {"id": c.job.id}) > c.claim.generation
    with pytest.raises(GatewayError):
        await c.accounting.record_evidence(c.claim, Usage(1, 1), "success")


@pytest.mark.parametrize("actual,currency,reason", [(Decimal(-1), "USD", "invoice"), (Decimal("NaN"), "USD", "invoice"),
    (Decimal("0.0000000000001"), "USD", "invoice"), (Decimal(1), "EUR", "invoice"),
    (Decimal(1), "USD", "Authorization: Bearer synthetic"), (Decimal(1), "USD", "synthetic-upstream-secret"),
    (Decimal(1), "USD", "bad\nreason"), (Decimal(1), "USD", "")])
async def test_reconcile_rejects_invalid_money_and_sensitive_reason(pg_db, actual, currency, reason):
    c = await case(pg_db)
    await c.accounting.reserve(c.claim, c.quote)
    await c.accounting.mark_dispatched(c.claim)
    await c.accounting.record_evidence(c.claim, None, "uncertain")
    view = await c.accounting.settle(c.job.id, claim=c.claim)
    with pytest.raises(GatewayError):
        await c.accounting.reconcile(c.job.id, view.version, actual, currency, reason, "admin")
    assert (await c.ops.get(c.job.id)).state == "usage_pending"
    assert await c.budgets.balance(c.budget) == (Decimal(0), Decimal("0.001048"), Decimal(10))


async def test_conflicting_evidence_cannot_replace_durable_usage(pg_db):
    c = await case(pg_db)
    await c.accounting.reserve(c.claim, c.quote)
    await c.accounting.mark_dispatched(c.claim)
    await c.accounting.record_evidence(c.claim, Usage(10, 2), "success")
    await c.accounting.record_evidence(c.claim, Usage(10, 2), "success")
    with pytest.raises(GatewayError, match="payload_mismatch"):
        await c.accounting.record_evidence(c.claim, None, "rejected")
    assert (await c.ops.get(c.job.id)).usage == Usage(10, 2)


async def test_reserve_rechecks_both_stamps_and_authorized_hold(pg_db):
    c = await case(pg_db)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE provider_operations SET input=jsonb_set(input,'{probe,max_hold}','\"1\"') WHERE id=:id"), {"id": c.job.id})
    with pytest.raises(GatewayError):
        await c.accounting.reserve(c.claim, c.quote)
    assert await c.budgets.balance(c.budget) == (Decimal(0), Decimal(0), Decimal(10))


async def test_pending_settlement_replays_for_same_claim_and_retains_unknown_usage(pg_db):
    c = await case(pg_db)
    await c.accounting.reserve(c.claim, c.quote)
    await c.accounting.mark_dispatched(c.claim)
    await c.accounting.record_evidence(c.claim, None, "uncertain")
    views = await asyncio.gather(*(c.accounting.settle(c.job.id, claim=c.claim) for _ in range(2)))
    assert views[0] == views[1]
    assert views[0].usage is None and views[0].state == "usage_pending"
    adjusted = await c.accounting.reconcile(c.job.id, views[0].version, Decimal("0.1"), "USD", "Invoice checked", "admin")
    assert adjusted.usage is None
    assert await counts(pg_db) == [0, 0, 0, 0]


async def test_recovery_waits_for_grace_and_owner_cleanup_survives_cancellation(pg_db):
    c = await case(pg_db)
    await c.accounting.reserve(c.claim, c.quote)
    await c.accounting.mark_dispatched(c.claim)
    await c.ops.cancel(c.job.id, (await c.ops.get(c.job.id)).version, "admin")
    await c.accounting.record_evidence(c.claim, Usage(10, 2), "success")
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE provider_operations SET deadline=clock_timestamp()-interval '10 seconds' WHERE id=:id"), {"id": c.job.id})
    with pytest.raises(GatewayError, match="claim_lost"):
        await c.accounting.settle(c.job.id)
    with pytest.raises(GatewayError, match="operation_expired"):
        await c.accounting.settle(c.job.id, claim=c.claim)
    assert await c.budgets.balance(c.budget) == (Decimal(0), Decimal("0.001048"), Decimal(10))


@pytest.mark.parametrize("field,value", [("max_hold", '"0"'), ("acknowledged", 'false'),
    ("currency", '"EUR"'), ("quote_digest", '"' + 'a'*64 + '"'),
    ("expected,budget_version", '9')])
async def test_intent_mismatch_is_rejected_without_money_writes(pg_db, field, value):
    c = await case(pg_db)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE provider_operations SET input=jsonb_set(input,CAST(:path AS text[]),CAST(:value AS jsonb)) WHERE id=:id"),
                              {"id": c.job.id, "path": "{probe," + field + "}", "value": value})
    with pytest.raises(GatewayError):
        await c.accounting.reserve(c.claim, c.quote)
    assert await c.budgets.balance(c.budget) == (Decimal(0), Decimal(0), Decimal(10))


async def test_settlement_audit_failure_rolls_back_money_and_evidence_survives(pg_db):
    c = await case(pg_db)
    await c.accounting.reserve(c.claim, c.quote)
    await c.accounting.mark_dispatched(c.claim)
    await c.accounting.record_evidence(c.claim, Usage(10, 2), "success")
    async with pg_db.sessions.begin() as session:
        await session.execute(text("ALTER TABLE audit_events ADD CONSTRAINT test_fail_settle_audit CHECK (action!='catalog.probe.settled')"))
    with pytest.raises(Exception):
        await c.accounting.settle(c.job.id, claim=c.claim)
    view = await c.ops.get(c.job.id)
    assert view.state == "running" and view.usage == Usage(10, 2)
    assert await c.budgets.balance(c.budget) == (Decimal(0), Decimal("0.001048"), Decimal(10))


async def test_provider_cost_over_hold_is_recorded_without_clamping_or_reenable(pg_db):
    c = await case(pg_db)
    await c.accounting.reserve(c.claim, c.quote)
    await c.accounting.mark_dispatched(c.claim)
    await c.accounting.record_evidence(c.claim, Usage(2000, 16), "success")
    view = await c.accounting.settle(c.job.id, claim=c.claim)
    assert view.cost == Decimal("0.002048")
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT config->'enabled' FROM model_bindings WHERE id=:id"), {"id": c.binding.id}) is False
        assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='catalog.probe.cost_overrun' AND record_id=:id"), {"id": c.job.id}) == 1
        assert await session.scalar(text("SELECT settlement_source FROM provider_operations WHERE id=:id"), {"id": c.job.id}) == "provider_estimated"


async def test_definite_rejection_evidence_is_compatible_with_recovery_truth_table(pg_db):
    c = await case(pg_db)
    await c.accounting.reserve(c.claim, c.quote)
    await c.accounting.mark_dispatched(c.claim)
    await c.accounting.record_evidence(c.claim, None, "rejected")
    assert (await c.ops.get(c.job.id)).result.get("rejected_before_generation") is True


async def test_cost_digest_changes_with_effective_proxy_rotation(pg_db):
    from autobuild_json.gateway.admin.schemas import ProxyInput
    from autobuild_json.gateway.proxy.profiles import ProfileStore
    c = await case(pg_db)
    profiles = ProfileStore(pg_db, synthetic_vault(), b"p"*32)
    profile = await profiles.create(ProxyInput(name="Synthetic", mode="fixed", entries_text="http://93.184.216.34:39008"))
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE credentials SET profile_id=:profile WHERE id=:id"), {"profile": profile, "id": c.source.credential_id})
    changed = await c.quotes.quote(c.source.id, c.binding.id)
    assert changed.digest != c.quote.digest and changed.stamp.proxy_id == profile
    with pytest.raises(GatewayError, match="version_conflict"):
        await c.accounting.reserve(c.claim, c.quote)
    assert await c.budgets.balance(c.budget) == (Decimal(0), Decimal(0), Decimal(10))
