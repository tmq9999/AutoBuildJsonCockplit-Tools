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
