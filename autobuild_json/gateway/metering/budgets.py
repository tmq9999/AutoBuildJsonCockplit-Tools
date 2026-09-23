"""Per-attempt upstream money holds; never modify a customer's token ledger."""
from decimal import Decimal, localcontext
import re
from uuid import uuid4

from sqlalchemy import text

from ..errors import GatewayError


def money(value):
    if not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value < Decimal("1e26"):
        raise GatewayError("invalid_request")
    with localcontext() as ctx:
        ctx.prec = 80
        if value * 10**12 != (value * 10**12).to_integral_value():
            raise GatewayError("invalid_request")
    return value


class BudgetService:
    def __init__(self, db):
        self.db = db

    async def create(self, currency, limit):
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise GatewayError("invalid_request")
        if limit is not None:
            money(limit)
        identity = uuid4()
        async with self.db.sessions.begin() as session:
            await session.execute(text("INSERT INTO upstream_budgets(id,currency,budget_limit) VALUES (:id,:currency,:limit)"),
                                  {"id": identity, "currency": currency, "limit": limit})
        return identity

    async def reserve(self, budget_id, attempt_id, currency, amount):
        money(amount)
        async with self.db.sessions.begin() as session:
            budget = (await session.execute(text("SELECT * FROM upstream_budgets WHERE id=:id FOR UPDATE"),
                                            {"id": budget_id})).mappings().first()
            if budget is None:
                raise GatewayError("not_found", 404)
            if budget["currency"] != currency:
                raise GatewayError("invalid_request")
            duplicate = await session.scalar(text("SELECT attempt_id FROM upstream_reservations WHERE attempt_id=:id"),
                                             {"id": attempt_id})
            if duplicate:
                raise GatewayError("duplicate_request", 409)
            if budget["budget_limit"] is not None and budget["held"] + budget["spent"] + amount > budget["budget_limit"]:
                raise GatewayError("budget_exceeded", 503, "quota")
            await session.execute(text("UPDATE upstream_budgets SET held=held+:amount WHERE id=:id"),
                                  {"id": budget_id, "amount": amount})
            await session.execute(text("INSERT INTO upstream_reservations(attempt_id,budget_id,amount) "
                                       "VALUES (:attempt,:budget,:amount)"),
                                  {"attempt": attempt_id, "budget": budget_id, "amount": amount})

    async def settle(self, attempt_id, actual):
        if actual is not None:
            money(actual)
        async with self.db.sessions.begin() as session:
            budget_id = await session.scalar(text("SELECT budget_id FROM upstream_reservations WHERE attempt_id=:id"),
                                             {"id": attempt_id})
            if budget_id is None:
                raise GatewayError("not_found", 404)
            await session.execute(text("SELECT id FROM upstream_budgets WHERE id=:id FOR UPDATE"), {"id": budget_id})
            row = (await session.execute(text("SELECT * FROM upstream_reservations WHERE attempt_id=:id FOR UPDATE"),
                                         {"id": attempt_id})).mappings().one()
            if row["state"] == "settled":
                return
            if actual is None:
                await session.execute(text("UPDATE upstream_reservations SET state='pending' WHERE attempt_id=:id"),
                                      {"id": attempt_id})
                return
            await session.execute(text("UPDATE upstream_budgets SET held=held-:hold,spent=spent+:actual WHERE id=:id"),
                                  {"id": budget_id, "hold": row["amount"], "actual": actual})
            await session.execute(text("UPDATE upstream_reservations SET state='settled',actual=:actual WHERE attempt_id=:id"),
                                  {"id": attempt_id, "actual": actual})

    async def balance(self, budget_id):
        async with self.db.sessions() as session:
            row = (await session.execute(text("SELECT spent,held,budget_limit FROM upstream_budgets WHERE id=:id"),
                                         {"id": budget_id})).first()
            if row is None:
                raise GatewayError("not_found", 404)
            return tuple(row)
