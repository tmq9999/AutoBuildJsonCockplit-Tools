"""Local-only quotes for one explicitly authorized, bounded probe."""

from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING, localcontext
import hashlib

from sqlalchemy import text

from ..errors import GatewayError
from ..metering.budgets import money
from ..metering.costs import CostSchedule
from .digests import canonical_bytes
from .records import ProbeQuote

PROMPT = "Reply with OK."
OUTPUT_BOUND = 16


def upper_cost(input_bound: int, schedule: CostSchedule) -> Decimal:
    if type(input_bound) is not int or not 0 < input_bound < 2**63:
        raise GatewayError("invalid_request")
    with localcontext() as ctx:
        ctx.prec = 80
        rates = [schedule.input_per_million]
        rates += [rate for rate in (schedule.cache_read_per_million,
                                    schedule.cache_write_per_million) if rate is not None]
        amount = (input_bound * max(rates) + OUTPUT_BOUND * schedule.output_per_million) / Decimal(1000000)
        return money(amount.quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING))


class ProbeQuotes:
    def __init__(self, db, sources):
        self.db, self.sources = db, sources

    async def quote(self, source_id, binding_id) -> ProbeQuote:
        async with self.db.sessions.begin() as session:
            context = await self.sources.lock_context(session, source_id)
            return await self.quote_in(session, context, binding_id)

    async def quote_in(self, session, context, binding_id) -> ProbeQuote:
        """Caller holds source dependencies; an operation lock precedes this for reserve."""
        row = (await session.execute(text("SELECT * FROM model_bindings WHERE id=:id FOR UPDATE"),
                                     {"id": binding_id})).mappings().first()
        if row is None:
            raise GatewayError("not_found", 404)
        if (row["provider_id"] != context.source.provider_id or
                row["credential_id"] != context.source.credential_id):
            raise GatewayError("invalid_request")
        binding = row["config"]
        if (type(binding.get("input_bound")) is not int or not 0 < binding["input_bound"] < 2**63
                or type(binding.get("output_bound")) is not int or not 16 <= binding["output_bound"] < 2**63
                or "text" not in binding.get("capabilities", ())):
            raise GatewayError("unsupported_feature")
        schedule, budget_id = context.provider.cost_schedule, context.provider.budget_id
        if schedule is None or budget_id is None:
            raise GatewayError("probe_budget_required")
        budget = (await session.execute(text("SELECT * FROM upstream_budgets WHERE id=:id FOR UPDATE"),
                                        {"id": budget_id})).mappings().first()
        if budget is None or budget["currency"] != schedule.currency:
            raise GatewayError("probe_budget_required")
        stamp = context.stamp.model_copy(update={"binding_id": binding_id, "binding_version": row["version"],
                                                "budget_id": budget_id, "budget_version": budget["version"]})
        hold = upper_cost(binding["input_bound"], schedule)
        payload = {"stamp": stamp.model_dump(mode="json"), "binding": binding,
                   "cost_schedule": asdict(schedule), "max_hold": hold, "currency": schedule.currency,
                   "budget_limit": budget["budget_limit"], "prompt": PROMPT, "output_bound": OUTPUT_BOUND}
        return ProbeQuote(stamp=stamp, input_bound=binding["input_bound"], cost_schedule=schedule,
                          upper_cost=hold, currency=schedule.currency,
                          digest=hashlib.sha256(canonical_bytes(payload)).hexdigest())
