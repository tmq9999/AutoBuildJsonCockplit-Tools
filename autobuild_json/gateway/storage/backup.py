"""Bounded encrypted logical snapshots; restore never overwrites populated tables."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text

from ..errors import GatewayError

TABLES = ("customers", "api_keys", "audit_events", "providers", "config_versions", "credentials", "public_models",
          "model_aliases", "model_bindings", "discovered_models", "quota_buckets", "requests", "attempts", "usage_ledger",
          "upstream_budgets", "upstream_reservations", "proxy_profiles", "proxy_leases", "proxy_health",
          "continuation_handles", "provider_admissions", "catalog_sources", "provider_operations",
          "provider_operation_claims", "catalog_entries", "catalog_decisions", "catalog_publications")
TABLES = TABLES + ("codex_account_state", "codex_reset_requests", "account_pool_policies",
                   "account_session_bindings", "key_quota_adjustments")
MAX_BACKUP = 64*1024*1024


class BackupService:
    def __init__(self, db, vault):
        self.db, self.vault = db, vault

    def _key_check(self):
        return hmac.new(self.vault.derive_key("client_keys", "client-key-hmac"), b"abgw:backup-key-check", hashlib.sha256).hexdigest()

    async def export(self, path):
        tables = {}
        async with self.db.engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="REPEATABLE READ")
            async with connection.begin():
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                total = 0
                for table in TABLES:
                    rows = []
                    stream = await connection.stream(text(f'SELECT row_to_json(t)::text FROM "{table}" t'))
                    async for row in stream:
                        total += len(row[0].encode())
                        if total > MAX_BACKUP:
                            raise GatewayError("invalid_request", 413)
                        rows.append(json.loads(row[0]))
                    tables[table] = rows
        plaintext = json.dumps({"format": 1, "revision": revision, "key_check": self._key_check(), "tables": tables},
                               separators=(",", ":")).encode()
        if len(plaintext) > MAX_BACKUP:
            raise GatewayError("invalid_request", 413)
        nonce = secrets.token_bytes(12)
        key_id = self.vault.active
        encrypted = AESGCM(self.vault.derive_key(key_id, "backup-v1")).encrypt(nonce, plaintext, ("abgw:backup:v1:"+key_id).encode())
        data = json.dumps({"key_id": key_id, "nonce": base64.b64encode(nonce).decode(),
                           "ciphertext": base64.b64encode(encrypted).decode()}).encode()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())

    async def restore(self, path):
        try:
            path = Path(path)
            if path.is_symlink():
                raise ValueError()
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BACKUP*2:
                    raise ValueError()
                envelope = json.load(source)
            key_id = envelope["key_id"]
            plaintext = AESGCM(self.vault.derive_key(key_id, "backup-v1")).decrypt(
                base64.b64decode(envelope["nonce"], validate=True), base64.b64decode(envelope["ciphertext"], validate=True),
                ("abgw:backup:v1:"+key_id).encode())
            payload = json.loads(plaintext)
            if payload["format"] != 1 or set(payload["tables"]) != set(TABLES) or not hmac.compare_digest(payload["key_check"], self._key_check()):
                raise ValueError()
            for rows in payload["tables"].values():
                for row in rows:
                    for field in ("encrypted_secret", "encrypted_entries", "encrypted_upstream_id"):
                        if row.get(field):
                            self.vault.derive_key(row[field]["key_id"], "backup-v1")
        except Exception:
            raise GatewayError("secret_unavailable", 400, "storage") from None
        async with self.db.sessions.begin() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(98761325)"))
            if await session.scalar(text("SELECT version_num FROM alembic_version")) != payload["revision"]:
                raise GatewayError("invalid_state", 409)
            for table in TABLES:
                # Explicit locks prevent a concurrent writer filling an empty target
                # between this check and the inserts in the same transaction.
                await session.execute(text(f'LOCK TABLE "{table}" IN ACCESS EXCLUSIVE MODE'))
                if await session.scalar(text(f'SELECT EXISTS(SELECT 1 FROM "{table}")')):
                    raise GatewayError("invalid_state", 409)
            # Source snapshot pointers refer back to operations. All other FKs
            # remain immediate while this same-revision restore inserts rows.
            await session.execute(text("SET CONSTRAINTS catalog_sources_latest_run, "
                                       "catalog_sources_previous_run DEFERRED"))
            for table in TABLES:
                for row in payload["tables"][table]:
                    await session.execute(text(f'INSERT INTO "{table}" SELECT * FROM json_populate_record(NULL::"{table}", CAST(:row AS json))'),
                                          {"row": json.dumps(row)})
            # Restoring a snapshot cannot resurrect a worker lease or replay a
            # potentially submitted redemption. Preserve unknown active locks.
            await session.execute(text("UPDATE codex_account_state SET generation=generation+1,refresh_deadline=NULL"))
            rows = (await session.execute(text("""UPDATE codex_reset_requests SET
                state=CASE WHEN state='prepared' THEN 'rejected' WHEN state='dispatched' THEN 'unknown' ELSE state END,
                result_code=CASE WHEN state='prepared' THEN 'operation_expired'
                    WHEN state='dispatched' THEN 'codex_reset_uncertain' ELSE result_code END,
                generation=generation+1,version=version+1,completed_at=clock_timestamp()
                WHERE state IN ('prepared','dispatched') RETURNING *"""))).mappings().all()
            from ..accounts.quota_storage import audit
            for row in rows:
                await audit(session, row, "restored", actor="system")
            rows = (await session.execute(text("UPDATE codex_reset_requests SET generation=generation+1,version=version+1 "
                "WHERE state='succeeded_refresh_failed' RETURNING *"))).mappings().all()
            for row in rows:
                await audit(session, row, "restore_fenced", actor="system")
