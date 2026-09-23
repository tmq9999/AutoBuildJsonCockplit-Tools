from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
from uuid import UUID, uuid4

from sqlalchemy import text

from ..errors import GatewayError
from ..secrets import Ciphertext


@dataclass(frozen=True)
class ContinuationScope:
    customer_id: UUID
    key_id: UUID
    public_model_id: str


@dataclass(frozen=True)
class ContinuationBinding:
    route_id: UUID
    credential_id: UUID
    upstream_id: str
    expires_at: datetime


class ContinuationStore:
    def __init__(self, db, vault):
        self.db, self.vault = db, vault

    async def issue(self, scope, binding):
        now = datetime.now(timezone.utc)
        if binding.expires_at.tzinfo is None or binding.expires_at <= now:
            raise GatewayError("invalid_state", 400)
        handle, identity = "resp_"+secrets.token_urlsafe(32), uuid4()
        encrypted = self.vault.seal("continuation", identity, binding.upstream_id.encode()).to_dict()
        async with self.db.sessions.begin() as session:
            await session.execute(text("""INSERT INTO continuation_handles
                (id,handle_digest,customer_id,key_id,model_id,route_id,credential_id,encrypted_upstream_id,expires_at)
                VALUES (:id,:digest,:customer,:key,:model,:route,:credential,CAST(:cipher AS jsonb),:expires)"""),
                {"id": identity, "digest": hashlib.sha256(handle.encode()).digest(), "customer": scope.customer_id,
                 "key": scope.key_id, "model": scope.public_model_id, "route": binding.route_id,
                 "credential": binding.credential_id, "cipher": json.dumps(encrypted),
                 "expires": min(binding.expires_at, now+timedelta(hours=24))})
        return handle

    async def resolve(self, handle, scope):
        if not isinstance(handle, str) or not handle.startswith("resp_") or len(handle) > 100:
            raise GatewayError("invalid_state")
        async with self.db.sessions() as session:
            row = (await session.execute(text("SELECT * FROM continuation_handles WHERE handle_digest=:digest "
                "AND customer_id=:customer AND key_id=:key AND model_id=:model AND expires_at>now()"),
                {"digest": hashlib.sha256(handle.encode()).digest(), "customer": scope.customer_id,
                 "key": scope.key_id, "model": scope.public_model_id})).mappings().first()
        if not row:
            raise GatewayError("invalid_state")
        raw = self.vault.open("continuation", row["id"], Ciphertext.from_dict(row["encrypted_upstream_id"])).decode()
        return ContinuationBinding(row["route_id"], row["credential_id"], raw, row["expires_at"])
