from contextlib import asynccontextmanager
from uuid import uuid4

from sqlalchemy import text

from ..errors import GatewayError


class ProviderLimits:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def acquire(self, route, deadline):
        identity = uuid4()
        async with self.db.sessions.begin() as session:
            provider = (
                (
                    await session.execute(
                        text("SELECT config,version FROM providers WHERE id=:id FOR UPDATE"),
                        {"id": route.provider_id},
                    )
                )
                .mappings()
                .first()
            )
            credential = (
                (
                    await session.execute(
                        text("SELECT * FROM credentials WHERE id=:id AND provider_id=:provider FOR UPDATE"),
                        {"id": route.credential_id, "provider": route.provider_id},
                    )
                )
                .mappings()
                .first()
            )
            if (
                not provider
                or not credential
                or not provider["config"]["enabled"]
                or not credential["enabled"]
            ):
                raise GatewayError("upstream_unavailable", 503)
            for column, record_id, rpm, concurrency in (
                (
                    "provider_id",
                    route.provider_id,
                    provider["config"].get("rpm_limit", 600),
                    provider["config"].get("concurrency_limit", 16),
                ),
                (
                    "credential_id",
                    route.credential_id,
                    credential["rpm_limit"],
                    credential["concurrency_limit"],
                ),
            ):
                counts = (
                    (
                        await session.execute(
                            text(
                                f"SELECT count(*) FILTER(WHERE admitted_at>now()-interval '60 seconds') AS rpm, "
                                f"count(*) FILTER(WHERE active AND deadline>now()) AS concurrent FROM provider_admissions WHERE {column}=:id"
                            ),
                            {"id": record_id},
                        )
                    )
                    .mappings()
                    .one()
                )
                if counts["rpm"] >= rpm or counts["concurrent"] >= concurrency:
                    raise GatewayError("upstream_unavailable", 503, "upstream", 1)
            await session.execute(
                text(
                    "INSERT INTO provider_admissions(id,provider_id,credential_id,deadline) VALUES (:id,:provider,:credential,:deadline)"
                ),
                {
                    "id": identity,
                    "provider": route.provider_id,
                    "credential": route.credential_id,
                    "deadline": deadline,
                },
            )
        try:
            yield identity
        finally:
            async with self.db.sessions.begin() as session:
                await session.execute(
                    text("UPDATE provider_admissions SET active=false WHERE id=:id"), {"id": identity}
                )
