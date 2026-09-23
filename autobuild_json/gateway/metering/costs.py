"""Provider cost is independent of customer weighted-token quota."""
from dataclasses import dataclass
from decimal import Decimal, localcontext
import re


@dataclass(frozen=True)
class CostSchedule:
    currency: str
    input_per_million: Decimal
    output_per_million: Decimal
    cache_read_per_million: Decimal | None = None
    cache_write_per_million: Decimal | None = None

    def __post_init__(self):
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("invalid_currency")
        for value in (self.input_per_million, self.output_per_million,
                      self.cache_read_per_million, self.cache_write_per_million):
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite()
                                      or value < 0 or value >= Decimal("1e20")):
                raise ValueError("invalid_price")


@dataclass(frozen=True)
class CostEstimate:
    amount: Decimal
    currency: str
    source: str = "estimated"


def estimate_cost(usage, schedule):
    if usage is None or schedule is None:
        return None
    if (usage.cached_read and schedule.cache_read_per_million is None
            or usage.cached_write and schedule.cache_write_per_million is None):
        return None
    with localcontext() as context:
        context.prec = 80
        uncached = usage.input_tokens - usage.cached_read - usage.cached_write
        amount = (uncached * schedule.input_per_million + usage.output_tokens * schedule.output_per_million
                  + usage.cached_read * (schedule.cache_read_per_million or Decimal(0))
                  + usage.cached_write * (schedule.cache_write_per_million or Decimal(0))) / 1_000_000
    return CostEstimate(amount, schedule.currency)
