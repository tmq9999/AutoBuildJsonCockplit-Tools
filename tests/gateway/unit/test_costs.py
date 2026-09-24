from decimal import Decimal

import pytest


def test_costs_split_cache_buckets_without_double_counting():
    from autobuild_json.gateway.metering.costs import CostSchedule, estimate_cost
    from autobuild_json.gateway.metering.records import Usage
    schedule = CostSchedule("USD", Decimal("1"), Decimal("3"), Decimal("0.1"), Decimal("2"))
    cost = estimate_cost(Usage(100, 50, cached_read=70, cached_write=10), schedule)
    assert cost.amount == Decimal("0.000197")
    assert cost.currency == "USD" and cost.source == "estimated"


def test_missing_cost_or_usage_stays_unknown():
    from autobuild_json.gateway.metering.costs import CostSchedule, estimate_cost
    from autobuild_json.gateway.metering.records import Usage
    schedule = CostSchedule("USD", Decimal("1"), Decimal("3"))
    assert estimate_cost(None, schedule) is None
    assert estimate_cost(Usage(1, 1), None) is None
    assert estimate_cost(Usage(100, 1, cached_read=50), schedule) is None


@pytest.mark.parametrize("amount", [Decimal("NaN"), Decimal("-1"), 1.1])
def test_cost_schedule_rejects_nonexact_or_negative_prices(amount):
    from autobuild_json.gateway.metering.costs import CostSchedule
    with pytest.raises(ValueError):
        CostSchedule("USD", amount, Decimal("1"))


def test_customer_cache_rates_do_not_change_upstream_cost():
    from autobuild_json.gateway.metering.costs import CostSchedule, estimate_cost
    from autobuild_json.gateway.metering.records import Usage
    from autobuild_json.gateway.metering.units import weighted_usage_micro

    usage = Usage(100, 50, cached_read=70, cached_write=10)
    schedule = CostSchedule("USD", Decimal("1"), Decimal("3"), Decimal("0.1"), Decimal("2"))
    assert weighted_usage_micro(usage, 1_000_000, 3_000_000, 0, 0) == 170_000_000
    assert estimate_cost(usage, schedule).amount == Decimal("0.000197")
