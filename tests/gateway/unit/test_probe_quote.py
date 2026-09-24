from decimal import Decimal, localcontext
from uuid import uuid4

import pytest
from pydantic import ValidationError

from autobuild_json.gateway.catalog.records import ConfigStamp, ProbeIntent
from autobuild_json.gateway.metering.costs import CostSchedule


def test_probe_upper_cost_is_exact_and_rounded_up():
    from autobuild_json.gateway.catalog.probe_quote import upper_cost
    with localcontext() as ctx:
        ctx.prec = 6
        assert upper_cost(1000, CostSchedule("USD", Decimal("1"), Decimal("3"))) == Decimal("0.001048000000")
        assert upper_cost(1, CostSchedule("USD", Decimal("0.0000001"), Decimal(0))) == Decimal("0.000000000001")
        assert upper_cost(1000, CostSchedule("USD", Decimal(1), Decimal(3), Decimal(9))) == Decimal("0.009048000000")


def test_zero_hold_requires_explicit_boolean_acknowledgement():
    values = dict(binding_id=uuid4(), quote_digest="a"*64, expected=ConfigStamp(
        source_version=1, provider_version=1, credential_version=1, content_digest="b"*64),
        max_hold=Decimal(0), currency="USD")
    assert ProbeIntent(**values, acknowledged=True).max_hold == 0
    for ack in (None, False, 1, "true"):
        with pytest.raises(ValidationError):
            ProbeIntent(**values, **({} if ack is None else {"acknowledged": ack}))


@pytest.mark.parametrize("amount", [Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), Decimal("1e26"), Decimal("0.0000000000001")])
def test_intent_rejects_unstorable_money(amount):
    with pytest.raises(ValidationError):
        ProbeIntent(binding_id=uuid4(), quote_digest="a"*64, expected=ConfigStamp(
            source_version=1, provider_version=1, credential_version=1, content_digest="b"*64),
            max_hold=amount, currency="USD", acknowledged=True)
