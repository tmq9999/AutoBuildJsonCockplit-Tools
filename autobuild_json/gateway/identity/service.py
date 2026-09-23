import hmac
from uuid import uuid4

from sqlalchemy import insert, select, update, func

from ..errors import GatewayError
from ..secrets import issue_key, key_digest
from ..storage.schema import customers, api_keys, audit_events
from .policy import IssuedKey, KeyPolicy, KeyView, Principal


def key_view(row):
    return KeyView(key_id=row["id"], customer_id=row["customer_id"], prefix=row["prefix"],
                   name=row["name"], policy=KeyPolicy.model_validate(row["policy"]),
                   version=row["version"], revoked_at=row["revoked_at"])


class IdentityService:
    def __init__(self, db, pepper: bytes, *, actor="admin"):
        if not isinstance(pepper, bytes) or len(pepper) < 32:
            raise GatewayError("invalid_keyring", 503, "auth")
        self.db, self._pepper, self.actor = db, pepper, actor

    async def _audit(self, session, action, identity, details=None):
        await session.execute(insert(audit_events).values(id=uuid4(), actor=self.actor,
            action=action, record_id=identity, details=details or {}))

    async def create_customer(self, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200 or any(ord(c) < 32 for c in name):
            raise GatewayError("invalid_request")
        identity = uuid4()
        async with self.db.sessions.begin() as session:
            await session.execute(insert(customers).values(id=identity, name=name.strip()))
            await self._audit(session, "customer.created", identity)
        return identity

    async def set_customer_enabled(self, customer_id, enabled, *, version):
        if type(enabled) is not bool:
            raise GatewayError("invalid_request")
        async with self.db.sessions.begin() as session:
            result = await session.execute(update(customers).where(customers.c.id == customer_id,
                customers.c.version == version).values(enabled=enabled, version=version + 1).returning(customers.c.id))
            if result.scalar_one_or_none() is None:
                raise GatewayError("version_conflict", 409)
            await self._audit(session, "customer.enabled" if enabled else "customer.disabled", customer_id)

    async def create_key(self, customer_id, policy: KeyPolicy):
        identity = uuid4()
        prefix, secret = issue_key()
        async with self.db.sessions.begin() as session:
            owner = (await session.execute(select(customers).where(customers.c.id == customer_id)
                                            .with_for_update())).mappings().first()
            if not owner or not owner["enabled"]:
                raise GatewayError("permission_denied", 403)
            await session.execute(insert(api_keys).values(id=identity, customer_id=customer_id,
                prefix=prefix, secret_digest=key_digest(secret, self._pepper), policy=policy.model_dump(mode="json"),
                enabled=policy.enabled, expires_at=policy.expires_at))
            await self._audit(session, "key.created", identity)
        return IssuedKey(identity, secret, 1)

    async def authenticate(self, secret, protocol):
        try:
            digest = key_digest(secret, self._pepper)
        except ValueError:
            raise GatewayError("invalid_api_key", 401, "auth") from None
        async with self.db.sessions() as session:
            query = (select(api_keys).join(customers, customers.c.id == api_keys.c.customer_id)
                .where(api_keys.c.secret_digest == digest, api_keys.c.enabled.is_(True),
                    customers.c.enabled.is_(True), api_keys.c.revoked_at.is_(None),
                    (api_keys.c.expires_at.is_(None) | (api_keys.c.expires_at > func.now()))))
            row = (await session.execute(query)).mappings().first()
            if not row or not hmac.compare_digest(row["secret_digest"], digest):
                raise GatewayError("invalid_api_key", 401, "auth")
            policy = KeyPolicy.model_validate(row["policy"])
            if protocol not in policy.protocols:
                raise GatewayError("permission_denied", 403, "policy")
            return Principal(row["customer_id"], row["id"], row["version"])

    async def _locked_key(self, session, key_id):
        owner_id = await session.scalar(select(api_keys.c.customer_id).where(api_keys.c.id == key_id))
        if owner_id is None:
            raise GatewayError("not_found", 404)
        await session.execute(select(customers.c.id).where(customers.c.id == owner_id).with_for_update())
        return (await session.execute(select(api_keys).where(api_keys.c.id == key_id)
                                      .with_for_update())).mappings().one()

    @staticmethod
    def _version(row, version):
        if row["revoked_at"] is not None:
            raise GatewayError("invalid_api_key", 401, "auth")
        if row["version"] != version:
            raise GatewayError("version_conflict", 409)

    async def rotate(self, key_id, version):
        prefix, secret = issue_key()
        async with self.db.sessions.begin() as session:
            row = await self._locked_key(session, key_id)
            self._version(row, version)
            await session.execute(update(api_keys).where(api_keys.c.id == key_id).values(
                prefix=prefix, secret_digest=key_digest(secret, self._pepper), version=version + 1))
            await self._audit(session, "key.rotated", key_id)
        return IssuedKey(key_id, secret, version + 1)

    async def revoke(self, key_id, version):
        async with self.db.sessions.begin() as session:
            row = await self._locked_key(session, key_id)
            if row["revoked_at"] is not None:
                return
            self._version(row, version)
            await session.execute(update(api_keys).where(api_keys.c.id == key_id).values(
                enabled=False, revoked_at=func.now(), version=version + 1))
            await self._audit(session, "key.revoked", key_id)

    async def update_policy(self, key_id, version, policy: KeyPolicy):
        async with self.db.sessions.begin() as session:
            row = await self._locked_key(session, key_id)
            self._version(row, version)
            updated = (await session.execute(update(api_keys).where(api_keys.c.id == key_id).values(
                policy=policy.model_dump(mode="json"), enabled=policy.enabled, expires_at=policy.expires_at,
                version=version + 1).returning(api_keys))).mappings().one()
            await self._audit(session, "key.policy_updated", key_id, {"version": version + 1})
            return key_view(updated)

    async def list_keys(self, customer_id):
        async with self.db.sessions() as session:
            rows = (await session.execute(select(api_keys).where(api_keys.c.customer_id == customer_id)
                                           .order_by(api_keys.c.created_at, api_keys.c.id))).mappings().all()
            return [key_view(row) for row in rows]
