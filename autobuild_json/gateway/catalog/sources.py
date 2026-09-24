"""Versioned source configuration and locked, live operation context."""

from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ..errors import GatewayError
from ..routing.records import ProviderConfig
from ..secrets import Ciphertext
from .context import context_digest, storage_fingerprint
from .records import ConfigStamp, OperationRoute, SourceContext, SourceInput, SourceView


_MODES = {
    "openai_compatible": frozenset({"openai_single", "openai_cursor"}),
    "anthropic": frozenset({"anthropic"}),
    "gemini": frozenset({"gemini"}),
    "ollama": frozenset({"ollama"}),
}


class _SkippedLock(Exception):
    pass


async def _locked_row(session, table: str, identity: UUID, *, skip_locked: bool):
    clause = "FOR UPDATE SKIP LOCKED" if skip_locked else "FOR UPDATE"
    row = (await session.execute(text(f"SELECT * FROM {table} WHERE id=:id {clause}"),
                                 {"id": identity})).mappings().first()
    if row is None and skip_locked:
        exists = await session.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {table} WHERE id=:id)"),
                                      {"id": identity})
        if exists:
            raise _SkippedLock()
    return row


def _view(row) -> SourceView:
    return SourceView.model_validate({name: row[name] for name in SourceView.model_fields})


class SourceRepository:
    def __init__(self, db, vault, pepper: bytes):
        if not isinstance(pepper, bytes) or len(pepper) < 32:
            raise ValueError("invalid_keyring")
        self.db, self.vault, self.pepper = db, vault, pepper

    async def _dependencies(self, session, provider_id: UUID, credential_id: UUID, *, skip_locked=False,
                            require_healthy=True, allow_missing_profile=False):
        # Every writer/context consumer follows provider -> credential -> effective
        # proxy. The source lock is acquired only after these rows are stable.
        provider_row = await _locked_row(session, "providers", provider_id, skip_locked=skip_locked)
        if provider_row is None:
            raise GatewayError("not_found", 404)
        credential_row = await _locked_row(session, "credentials", credential_id, skip_locked=skip_locked)
        if credential_row is None or credential_row["provider_id"] != provider_id:
            raise GatewayError("invalid_request")
        try:
            provider = ProviderConfig.model_validate(provider_row["config"])
        except ValidationError:
            raise GatewayError("invalid_state", 409) from None
        if require_healthy and (not provider.enabled or not credential_row["enabled"]
                                or credential_row["health"] != "active"):
            raise GatewayError("invalid_state", 409)
        if provider.adapter == "codex_oauth" or provider.auth_mode == "oauth":
            raise GatewayError("unsupported_feature")
        proxy_id = credential_row["profile_id"] or provider.proxy_profile_id
        proxy_row = None
        if proxy_id is not None:
            proxy_row = await _locked_row(session, "proxy_profiles", proxy_id, skip_locked=skip_locked)
            if proxy_row is None:
                if not allow_missing_profile:
                    raise GatewayError("invalid_state", 409)
        return provider_row, credential_row, provider, proxy_row, proxy_id

    @staticmethod
    def _mode(provider: ProviderConfig, mode):
        if mode.value not in _MODES.get(provider.adapter, ()):
            raise GatewayError("invalid_request")

    @staticmethod
    async def _audit(session, action: str, source_id: UUID, actor):
        await session.execute(text("INSERT INTO audit_events(id,actor,action,record_id) "
                                   "VALUES (:id,:actor,:action,:record)"),
                              {"id": uuid4(), "actor": str(actor), "action": action, "record": source_id})

    async def create(self, input: SourceInput, actor) -> SourceView:
        source_id = uuid4()
        try:
            async with self.db.sessions.begin() as session:
                _, _, provider, _, _ = await self._dependencies(session, input.provider_id, input.credential_id)
                self._mode(provider, input.mode)
                row = (await session.execute(text("""INSERT INTO catalog_sources
                    (id,provider_id,credential_id,mode,enabled,schedule_enabled,interval_seconds,next_run_at)
                    VALUES (:id,:provider,:credential,:mode,:enabled,:scheduled,:interval,
                      CASE WHEN :enabled AND :scheduled THEN clock_timestamp()+make_interval(secs=>:interval)
                           ELSE NULL END) RETURNING *"""),
                    {"id": source_id, "provider": input.provider_id, "credential": input.credential_id,
                     "mode": input.mode.value, "enabled": input.enabled, "scheduled": input.schedule_enabled,
                     "interval": input.interval_seconds})).mappings().one()
                await self._audit(session, "catalog.source.created", source_id, actor)
                return _view(row)
        except IntegrityError:
            raise GatewayError("version_conflict", 409) from None

    async def get(self, source_id: UUID) -> SourceView:
        async with self.db.sessions() as session:
            row = (await session.execute(text("SELECT * FROM catalog_sources WHERE id=:id"),
                                         {"id": source_id})).mappings().first()
        if row is None:
            raise GatewayError("not_found", 404)
        return _view(row)

    async def list(self) -> list[SourceView]:
        async with self.db.sessions() as session:
            rows = (await session.execute(text("SELECT * FROM catalog_sources ORDER BY created_at,id"))).mappings().all()
        return [_view(row) for row in rows]

    async def update(self, source_id: UUID, expected_version: int, input: SourceInput, actor) -> SourceView:
        async with self.db.sessions.begin() as session:
            current_read = (await session.execute(text("SELECT * FROM catalog_sources WHERE id=:id"),
                                                  {"id": source_id})).mappings().first()
            if current_read is None:
                raise GatewayError("not_found", 404)
            if current_read["version"] != expected_version:
                raise GatewayError("version_conflict", 409)
            if (current_read["provider_id"] != input.provider_id
                    or current_read["credential_id"] != input.credential_id):
                raise GatewayError("invalid_request")
            stopping = (not input.enabled or
                        (current_read["enabled"] and input.enabled and not input.schedule_enabled))
            _, _, provider, _, _ = await self._dependencies(
                session, input.provider_id, input.credential_id,
                require_healthy=not stopping, allow_missing_profile=stopping,
            )
            self._mode(provider, input.mode)
            current = (await session.execute(text("SELECT * FROM catalog_sources WHERE id=:id FOR UPDATE"),
                                             {"id": source_id})).mappings().first()
            if current is None or current["version"] != expected_version:
                raise GatewayError("version_conflict", 409)
            reschedule = (not current["schedule_enabled"] or not current["enabled"]
                          or current["interval_seconds"] != input.interval_seconds)
            row = (await session.execute(text("""UPDATE catalog_sources SET enabled=:enabled,mode=:mode,
                schedule_enabled=:scheduled,interval_seconds=:interval,version=version+1,
                next_run_at=CASE WHEN NOT :enabled OR NOT :scheduled THEN NULL
                  WHEN :reschedule THEN clock_timestamp()+make_interval(secs=>:interval)
                  ELSE next_run_at END,
                updated_at=clock_timestamp()
                WHERE id=:id AND version=:version RETURNING *"""),
                {"id": source_id, "version": expected_version, "enabled": input.enabled,
                 "mode": input.mode.value, "scheduled": input.schedule_enabled,
                 "interval": input.interval_seconds, "reschedule": reschedule})).mappings().first()
            if row is None:
                raise GatewayError("version_conflict", 409)
            await self._audit(session, "catalog.source.updated", source_id, actor)
            return _view(row)

    async def context(self, source_id: UUID) -> SourceContext:
        async with self.db.sessions.begin() as session:
            return await self.lock_context(session, source_id)

    async def lock_context(self, session, source_id: UUID, *, skip_locked: bool = False) -> SourceContext | None:
        if skip_locked:
            try:
                async with session.begin_nested():
                    return await self._load_context(session, source_id, skip_locked=True)
            except _SkippedLock:
                return None
        return await self._load_context(session, source_id, skip_locked=False)

    async def _load_context(self, session, source_id: UUID, *, skip_locked: bool) -> SourceContext:
        identity = (await session.execute(text("SELECT provider_id,credential_id FROM catalog_sources WHERE id=:id"),
                                          {"id": source_id})).mappings().first()
        if identity is None:
            raise GatewayError("not_found", 404)
        provider_row, credential_row, provider, proxy_row, proxy_id = await self._dependencies(
            session, identity["provider_id"], identity["credential_id"], skip_locked=skip_locked)
        source_row = await _locked_row(session, "catalog_sources", source_id, skip_locked=skip_locked)
        if source_row is None:
            raise GatewayError("invalid_state", 409)
        if (source_row["provider_id"] != provider_row["id"] or
                source_row["credential_id"] != credential_row["id"] or not source_row["enabled"]):
            raise GatewayError("invalid_state", 409)
        source = _view(source_row)
        self._mode(provider, source.mode)
        identity_config = {
            "provider_id": str(provider_row["id"]),
            "credential_id": str(credential_row["id"]),
            "root": provider.root,
            "adapter": provider.adapter,
            "wire_api": provider.wire_api,
            "auth_mode": provider.auth_mode,
            "credential_secret": storage_fingerprint(credential_row["encrypted_secret"], self.pepper),
            "token_generation": credential_row["token_generation"],
            "mode": source.mode.value,
            "proxy": None if proxy_row is None else {
                "id": str(proxy_id), "config": proxy_row["config"],
                "entries": storage_fingerprint({"raw": self.vault.open(
                    "proxy_profile", proxy_id, Ciphertext.from_dict(proxy_row["encrypted_entries"])
                ).decode("utf-8")}, self.pepper),
            },
        }
        stamp = ConfigStamp(source_version=source.version, provider_version=provider_row["version"],
                            credential_version=credential_row["version"], proxy_id=proxy_id,
                            proxy_version=proxy_row["version"] if proxy_row else None,
                            content_digest=context_digest(identity_config, self.pepper))
        route = OperationRoute(provider_id=provider_row["id"], credential_id=credential_row["id"],
                               root=provider.root, adapter=provider.adapter, wire_api=provider.wire_api,
                               auth_mode=provider.auth_mode, config_version=provider_row["version"],
                               proxy_profile_id=proxy_id, timeout=provider.timeout)
        return SourceContext(source=source, stamp=stamp, provider=provider, route=route,
                             effective_proxy_id=proxy_id)
