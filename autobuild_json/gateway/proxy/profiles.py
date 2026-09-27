import json
from uuid import uuid4

from sqlalchemy import text

from ...proxies import parse_proxies
from ..errors import GatewayError
from ..secrets import Ciphertext
from .config import ProxySelection, parse_kiot_keys


class ProfileStore:
    def __init__(self, db, vault, pepper):
        self.db, self.vault, self.pepper = db, vault, pepper

    def selection(self, mode, value, *, region="random", protocol="http", rotate=False, identity=None):
        try:
            entries = parse_kiot_keys(value, pepper=self.pepper) if mode == "kiotproxy" else parse_proxies(value)
            return ProxySelection(mode, identity if mode != "direct" else None, tuple(entries), region, protocol, rotate)
        except Exception:
            raise GatewayError("invalid_request") from None

    async def create(self, payload):
        raw = payload.entries_text.get_secret_value()
        self.selection(payload.mode, raw, region=payload.region, protocol=payload.protocol, rotate=payload.rotate)
        identity = uuid4()
        cipher = self.vault.seal("proxy_profile", identity, raw.encode()).to_dict()
        config = {"mode": payload.mode, "region": payload.region, "protocol": payload.protocol, "rotate": payload.rotate}
        async with self.db.sessions.begin() as session:
            await session.execute(text("INSERT INTO proxy_profiles(id,name,config,encrypted_entries) "
                "VALUES (:id,:name,CAST(:config AS jsonb),CAST(:cipher AS jsonb))"),
                {"id": identity, "name": payload.name, "config": json.dumps(config), "cipher": json.dumps(cipher)})
            await session.execute(text("INSERT INTO audit_events(id,actor,action,record_id) VALUES (:id,'admin','proxy.created',:record)"),
                                  {"id": uuid4(), "record": identity})
        return identity

    async def load(self, identity):
        async with self.db.sessions() as session:
            return await self.load_in_session(session, identity)

    async def load_in_session(self, session, identity):
        row = (await session.execute(text("SELECT * FROM proxy_profiles WHERE id=:id"), {"id": identity})).mappings().first()
        if row is None:
            raise GatewayError("not_found", 404)
        raw = self.vault.open("proxy_profile", identity, Ciphertext.from_dict(row["encrypted_entries"])).decode()
        return self.selection(value=raw, identity=identity, **row["config"])

    async def update(self, identity, version, payload):
        raw = payload.entries_text.get_secret_value()
        self.selection(payload.mode, raw, region=payload.region, protocol=payload.protocol, rotate=payload.rotate)
        cipher = self.vault.seal("proxy_profile", identity, raw.encode()).to_dict()
        config = {"mode": payload.mode, "region": payload.region, "protocol": payload.protocol, "rotate": payload.rotate}
        async with self.db.sessions.begin() as session:
            result = await session.scalar(text("UPDATE proxy_profiles SET name=:name,config=CAST(:config AS jsonb),"
                "encrypted_entries=CAST(:cipher AS jsonb),version=version+1 WHERE id=:id AND version=:version RETURNING id"),
                {"id": identity, "version": version, "name": payload.name, "config": json.dumps(config), "cipher": json.dumps(cipher)})
            if not result:
                raise GatewayError("version_conflict", 409)
            await session.execute(text("INSERT INTO audit_events(id,actor,action,record_id) VALUES (:id,'admin','proxy.updated',:record)"),
                                  {"id": uuid4(), "record": identity})

    async def list_profiles(self):
        async with self.db.sessions() as session:
            return [dict(row) for row in (await session.execute(text("SELECT id,name,config,version FROM proxy_profiles ORDER BY name"))).mappings()]
