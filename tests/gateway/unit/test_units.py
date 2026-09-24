import pytest


def test_weighted_usage_is_exact():
    from autobuild_json.gateway.metering.units import coefficient_micro, weighted_micro
    assert coefficient_micro("0.000001") == 1
    assert weighted_micro(1000, 500, coefficient_micro("1"), coefficient_micro("3")) == 2_500_000_000
    assert weighted_micro(1000, 500, 0, 0) == 0


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0.0000001", "1e10000", "foo", 1.0, True])
def test_coefficient_rejects_ambiguous_values(value):
    from autobuild_json.gateway.metering.units import coefficient_micro
    with pytest.raises(ValueError, match="invalid_coefficient"):
        coefficient_micro(value)


def test_usage_never_overflows_or_accepts_negative_tokens():
    from autobuild_json.gateway.metering.units import weighted_micro
    for args in [(-1, 0, 1, 1), (True, 1, 1, 1), (10**20, 0, 10**20, 0)]:
        with pytest.raises(ValueError):
            weighted_micro(*args)


def test_usage_subtotals_cannot_exceed_totals():
    from autobuild_json.gateway.metering.records import Usage
    for values in [{"cached_read": 101}, {"reasoning": 51}, {"input_tokens": -1}]:
        with pytest.raises(ValueError):
            Usage(**dict({"input_tokens": 100, "output_tokens": 50}, **values))


def test_hold_uses_highest_cache_rate_and_zero_is_free():
    from autobuild_json.gateway.metering.records import Bounds
    from autobuild_json.gateway.metering.units import hold_micro

    bounds = Bounds(100, 20)
    assert hold_micro(bounds, 1_000_000, 3_000_000, 2_000_000, 0) == 260_000_000
    assert hold_micro(bounds, 1_000_000, 3_000_000, 0, 0) == 160_000_000


@pytest.mark.parametrize("rate", [-1, True, 1.5, float("nan"), float("inf"), 10**38])
def test_new_cache_rates_reject_invalid_values(rate):
    from autobuild_json.gateway.metering.records import Bounds, Usage
    from autobuild_json.gateway.metering.units import hold_micro, weighted_usage_micro

    with pytest.raises(ValueError):
        weighted_usage_micro(Usage(1, 0, cached_read=1), 1, 0, rate)
    with pytest.raises(ValueError):
        hold_micro(Bounds(1, 0), 1, 0, cache_write_micro=rate)


def test_legacy_admission_constructor_keeps_cache_rates_optional():
    from datetime import datetime, timezone
    from uuid import uuid4

    from autobuild_json.gateway.identity.policy import Principal
    from autobuild_json.gateway.metering.records import Admission, Bounds

    admission = Admission(uuid4(), Principal(uuid4(), uuid4(), 1), "m", Bounds(1, 2),
                          3, 4, datetime.now(timezone.utc))
    assert (admission.cache_read_micro, admission.cache_write_micro) == (None, None)


@pytest.mark.parametrize("rate", [-1, True, 1.5, float("nan"), 10**38])
def test_admission_rejects_invalid_cache_rates(rate):
    from datetime import datetime, timezone
    from uuid import uuid4

    from autobuild_json.gateway.identity.policy import Principal
    from autobuild_json.gateway.metering.records import Admission, Bounds

    with pytest.raises(ValueError):
        Admission(uuid4(), Principal(uuid4(), uuid4(), 1), "m", Bounds(1, 0),
                  1, 0, datetime.now(timezone.utc), cache_read_micro=rate)
