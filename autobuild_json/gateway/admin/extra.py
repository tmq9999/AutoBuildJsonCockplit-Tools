from decimal import Decimal
from uuid import UUID

from pydantic import Field
from sqlalchemy import text

from ..errors import GatewayError
from .schemas import StrictInput, VersionInput


class AliasInput(StrictInput):
    alias: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=200)


class BudgetInput(StrictInput):
    currency: str
    limit: str | None


class ReleaseInput(StrictInput):
    key_index: int = Field(strict=True, ge=0, le=9999)


def mount_extra(router, services):
    @router.get("/aliases")
    async def aliases():
        async with services.db.sessions() as session:
            return [
                dict(row)
                for row in (
                    await session.execute(text("SELECT alias,model_id FROM model_aliases ORDER BY alias"))
                ).mappings()
            ]

    @router.post("/aliases", status_code=201)
    async def alias(payload: AliasInput):
        await services.catalog.put_alias(payload.alias, payload.model_id)
        return {"alias": payload.alias}

    @router.get("/budgets")
    async def budgets():
        async with services.db.sessions() as session:
            return [
                dict(
                    id=row["id"],
                    currency=row["currency"],
                    budget_limit=str(row["budget_limit"]) if row["budget_limit"] is not None else None,
                    spent=str(row["spent"]),
                    held=str(row["held"]),
                    version=row["version"],
                )
                for row in (
                    await session.execute(text("SELECT * FROM upstream_budgets ORDER BY id"))
                ).mappings()
            ]

    @router.post("/budgets", status_code=201)
    async def budget(payload: BudgetInput):
        return {
            "id": await services.budgets.create(
                payload.currency, Decimal(payload.limit) if payload.limit is not None else None
            )
        }

    @router.delete("/proxies/{identity}")
    async def delete_proxy(identity: UUID, payload: VersionInput):
        async with services.db.sessions.begin() as session:
            # Lock before checking references: account PATCH holds KEY SHARE
            # through reference creation, so READ COMMITTED sees its commit.
            version = await session.scalar(text("SELECT version FROM proxy_profiles WHERE id=:id FOR UPDATE"),
                                           {"id": identity})
            if version != payload.version:
                raise GatewayError("version_conflict", 409)
            if await session.scalar(
                text("SELECT count(*) FROM providers WHERE config->>'proxy_profile_id'=:id"),
                {"id": str(identity)},
            ) or await session.scalar(
                text("SELECT count(*) FROM credentials WHERE profile_id=:id"), {"id": identity}
            ):
                raise GatewayError("invalid_state", 409)
            result = await session.scalar(
                text("DELETE FROM proxy_profiles WHERE id=:id AND version=:version RETURNING id"),
                {"id": identity, "version": payload.version},
            )
            if not result:
                raise GatewayError("version_conflict", 409)
            await services.catalog._audit(session, "proxy.deleted", identity)
        return {"id": identity, "deleted": True}

    @router.post("/proxies/{identity}/release")
    async def release_proxy(identity: UUID, payload: ReleaseInput):
        selection = await services.profiles.load(identity)
        if selection.mode != "kiotproxy" or payload.key_index >= len(selection.runtime_entries):
            raise GatewayError("invalid_request")
        await services.proxies.release_kiot(selection.runtime_entries[payload.key_index], "admin")
        return {"released": True}

    @router.get("/key-balances")
    async def balances():
        async with services.db.sessions() as session:
            return [
                dict(row)
                for row in (
                    await session.execute(
                        text(
                            "SELECT key_id,window_kind,period,spent,held FROM quota_buckets WHERE window_kind='total'"
                        )
                    )
                ).mappings()
            ]
