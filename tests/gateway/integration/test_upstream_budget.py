import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_provider_budget_is_atomic_and_separate_from_client_quota(pg_db):
    from autobuild_json.gateway.metering.budgets import BudgetService
    from autobuild_json.gateway.errors import GatewayError
    service = BudgetService(pg_db)
    budget_id = await service.create("USD", Decimal("1"))
    a, b = uuid4(), uuid4()
    results = await asyncio.gather(service.reserve(budget_id, a, "USD", Decimal("0.7")),
                                  service.reserve(budget_id, b, "USD", Decimal("0.7")), return_exceptions=True)
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert sum(isinstance(r, GatewayError) and r.code == "budget_exceeded" for r in results) == 1
    accepted = a if not isinstance(results[0], Exception) else b
    await service.settle(accepted, Decimal("0.2"))
    await service.settle(accepted, Decimal("0.2"))
    balance = await service.balance(budget_id)
    assert balance == (Decimal("0.2"), Decimal("0"), Decimal("1"))


async def test_budget_unknown_cost_retains_hold_and_currency_cannot_mix(pg_db):
    from autobuild_json.gateway.metering.budgets import BudgetService
    from autobuild_json.gateway.errors import GatewayError
    service = BudgetService(pg_db)
    budget_id = await service.create("USD", Decimal("1"))
    attempt = uuid4()
    with pytest.raises(GatewayError, match="invalid_request"):
        await service.reserve(budget_id, attempt, "EUR", Decimal("0.5"))
    await service.reserve(budget_id, attempt, "USD", Decimal("0.5"))
    await service.settle(attempt, None)
    assert await service.balance(budget_id) == (Decimal("0"), Decimal("0.5"), Decimal("1"))


async def test_budget_overage_records_actual_cost_and_blocks_further_admission(pg_db):
    from autobuild_json.gateway.metering.budgets import BudgetService
    from autobuild_json.gateway.errors import GatewayError
    service = BudgetService(pg_db)
    budget_id = await service.create("USD", Decimal("1"))
    attempt = uuid4()
    await service.reserve(budget_id, attempt, "USD", Decimal("0.5"))
    await service.settle(attempt, Decimal("1.2"))
    assert await service.balance(budget_id) == (Decimal("1.2"), Decimal("0"), Decimal("1"))
    with pytest.raises(GatewayError, match="budget_exceeded"):
        await service.reserve(budget_id, uuid4(), "USD", Decimal("0.1"))
