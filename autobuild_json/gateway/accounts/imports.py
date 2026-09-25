import asyncio
from datetime import datetime, timedelta, timezone
import json
from uuid import UUID, uuid4

from sqlalchemy import text
from pydantic import ValidationError

from ...models import SuccessRecord
from ...oauth import ISSUER
from ..errors import GatewayError
from ..providers.catalog import Catalog
from ..routing.records import ProviderConfig
from ..proxy.pg_leases import PgLeaseStore
from ..secrets import Ciphertext
from .refresh import exchange_tokens, verify_tokens
from .proxy_selection import AccountProxyResolver

CODEX_PROVIDER = UUID("25b064da-1b7a-4ac2-9067-386f04d02a3e")


class CredentialService:
    def __init__(self, db, vault, proxies, transport, *, exchange=None, verifier=None, profile_resolver=None):
        self.db, self.vault, self.proxies, self.transport = db, vault, proxies, transport
        self.exchange, self.verifier = exchange or exchange_tokens, verifier or verify_tokens
        self.profile_resolver = profile_resolver
        self.refresh_leases = PgLeaseStore(db)

    async def validate_profile(self, profile_id):
        """Validate an optional profile reference before writing credentials."""
        if profile_id is None:
            return
        async with self.db.sessions() as session:
            exists = await session.scalar(text("SELECT 1 FROM proxy_profiles WHERE id=:id"), {"id": profile_id})
        if not exists:
            raise GatewayError("invalid_request", 422)

    async def import_record_result(self, record, profile_id):
        """Import one record and return ``(credential_id, created)``.

        The INSERT/ON CONFLICT operation and the follow-up lookup are in the
        same transaction.  Consequently a concurrent import of the same
        issuer/account is reported as a duplicate rather than being mistaken
        for a newly-created row.
        """
        try:
            parsed = SuccessRecord.model_validate(record)
        except ValidationError:
            raise GatewayError("invalid_request") from None
        identity = uuid4()
        encrypted = self.vault.seal("credential", identity, parsed.tokens.model_dump_json().encode()).to_dict()
        config = ProviderConfig(name="Codex OAuth", adapter="codex_oauth", root="https://chatgpt.com/backend-api/codex",
                                wire_api="responses", auth_mode="oauth")
        async with self.db.sessions.begin() as session:
            await session.execute(text("INSERT INTO providers(id,config) VALUES (:id,CAST(:config AS jsonb)) ON CONFLICT DO NOTHING"),
                                  {"id": CODEX_PROVIDER, "config": config.model_dump_json()})
            await session.execute(text("INSERT INTO config_versions(provider_id,version,config) VALUES (:id,1,CAST(:config AS jsonb)) "
                                       "ON CONFLICT DO NOTHING"), {"id": CODEX_PROVIDER, "config": config.model_dump_json()})
            # profile_id has no foreign key.  Cooperate with the profile
            # deletion lock so an import cannot leave a dangling reference.
            if profile_id is not None and not await session.scalar(text(
                    "SELECT id FROM proxy_profiles WHERE id=:id FOR KEY SHARE"), {"id": profile_id}):
                raise GatewayError("invalid_request", 422)
            inserted = await session.scalar(text("""INSERT INTO credentials
                (id,provider_id,encrypted_secret,health,issuer,account_id,email,profile_id)
                VALUES (:id,:provider,CAST(:cipher AS jsonb),'unverified',:issuer,:account,:email,:profile)
                ON CONFLICT(issuer,account_id) DO NOTHING RETURNING id"""),
                {"id": identity, "provider": CODEX_PROVIDER, "cipher": json.dumps(encrypted), "issuer": ISSUER,
                 "account": parsed.account.id, "email": parsed.email, "profile": profile_id})
            if inserted is None:
                existing = await session.scalar(text("SELECT id FROM credentials WHERE issuer=:issuer AND account_id=:account"),
                                                {"issuer": ISSUER, "account": parsed.account.id})
                return existing, False
            await Catalog(self.db, self.vault)._audit(session, "oauth.imported", identity)
        return identity, True

    async def import_record(self, record, profile_id):
        credential_id, _created = await self.import_record_result(record, profile_id)
        return credential_id

    async def _record(self, credential_id):
        async with self.db.sessions() as session:
            row = (await session.execute(text("SELECT * FROM credentials WHERE id=:id AND issuer=:issuer AND enabled"),
                                         {"id": credential_id, "issuer": ISSUER})).mappings().first()
        if row is None:
            raise GatewayError("not_found", 404)
        return row

    def _tokens(self, row):
        return json.loads(self.vault.open("credential", row["id"], Ciphertext.from_dict(row["encrypted_secret"])))

    async def commit_refresh(self, credential_id, expected_generation, owner, tokens, expires_in):
        if type(expires_in) is not int or not 1 <= expires_in <= 86400*30:
            raise GatewayError("refresh_uncertain", 503)
        if any(not isinstance(tokens.get(k), str) or not tokens[k] for k in ("id_token", "access_token", "refresh_token")):
            raise GatewayError("refresh_uncertain", 503)
        cipher = self.vault.seal("credential", credential_id,
            json.dumps({k: tokens[k] for k in ("id_token", "access_token", "refresh_token")}).encode()).to_dict()
        async with self.db.sessions.begin() as session:
            changed = await session.scalar(text("""UPDATE credentials SET encrypted_secret=CAST(:cipher AS jsonb),
                token_generation=token_generation+1,token_expires_at=now()+make_interval(secs=>:expiry),
                health='active',refresh_owner=NULL WHERE id=:id AND token_generation=:generation AND refresh_owner=:owner
                AND health='refreshing' RETURNING id"""),
                {"cipher": json.dumps(cipher), "expiry": expires_in, "id": credential_id,
                 "generation": expected_generation, "owner": owner})
            if not changed:
                raise GatewayError("invalid_state", 409)

    async def fresh_tokens(self, credential_id, deadline):
        owner, resource = uuid4(), "refresh:"+str(credential_id)
        token = None
        while datetime.now(timezone.utc) < deadline:
            row = await self._record(credential_id)
            if row["health"] in {"reauth_required", "refresh_uncertain"}:
                raise GatewayError(row["health"], 503, "refresh")
            if row["health"] == "active" and row["token_expires_at"] and row["token_expires_at"] > datetime.now(timezone.utc)+timedelta(seconds=60):
                return self._tokens(row)
            token = await self.refresh_leases.claim(resource, owner, deadline)
            if token:
                break
            await asyncio.sleep(0.05)
        if token is None:
            raise GatewayError("deadline_exceeded", 504, "refresh")
        try:
            row = await self._record(credential_id)
            if row['health'] in {'reauth_required','refresh_uncertain'}:
                raise GatewayError(row['health'],503,'refresh')
            if row["health"] == "active" and row["token_expires_at"] and row["token_expires_at"] > datetime.now(timezone.utc)+timedelta(seconds=60):
                return self._tokens(row)
            if row["health"] == "refreshing":
                await self._mark_uncertain(credential_id)
                raise GatewayError("refresh_uncertain", 503, "refresh")
            selection = await AccountProxyResolver(self.db, self.profile_resolver).resolve(credential_id)
            async with self.proxies.acquire(selection, owner, deadline) as lease:
                async with self.db.sessions.begin() as session:
                    await session.execute(text("UPDATE credentials SET health='refreshing',refresh_owner=:owner WHERE id=:id"),
                                          {"id": credential_id, "owner": owner})
                try:
                    fresh = await self.exchange(self._tokens(row), lease, deadline)
                    await self.verifier(fresh, row["account_id"], row["email"], lease, deadline)
                    if not await self.refresh_leases.assert_owner(token):
                        raise GatewayError("refresh_uncertain", 503)
                    await self.commit_refresh(credential_id, row["token_generation"], owner, fresh, fresh.get("expires_in"))
                    return {k: fresh[k] for k in ("id_token", "access_token", "refresh_token")}
                except BaseException as exc:
                    state = "reauth_required" if isinstance(exc, GatewayError) and exc.code == "reauth_required" else "refresh_uncertain"
                    async with self.db.sessions.begin() as session:
                        await session.execute(text("UPDATE credentials SET health=:state,refresh_owner=NULL WHERE id=:id AND refresh_owner=:owner"),
                                              {"id": credential_id, "owner": owner, "state": state})
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise GatewayError(state, 503, "refresh") from None
        finally:
            await self.refresh_leases.release(token)

    async def _mark_uncertain(self, credential_id):
        async with self.db.sessions.begin() as session:
            await session.execute(text("UPDATE credentials SET health='refresh_uncertain',refresh_owner=NULL WHERE id=:id"),
                                  {"id": credential_id})
