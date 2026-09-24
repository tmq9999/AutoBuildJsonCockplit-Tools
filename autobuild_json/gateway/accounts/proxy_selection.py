"""Shared credential > provider > direct account proxy policy."""
from ..errors import GatewayError
from ..proxy.config import ProxySelection
from . import quota_storage as storage


class AccountProxyResolver:
    def __init__(self, db, profiles):
        self.db, self.profiles = db, profiles

    async def policy(self, credential_id):
        # Keep the profile material and dependency stamp from the same locked
        # configuration. No lock survives this storage-only resolution.
        async with self.db.sessions.begin() as session:
            stamp = await storage.dependencies(session, credential_id)
            if stamp['proxy_id'] is None:
                selection = ProxySelection('direct')
            else:
                if self.profiles is None:
                    raise GatewayError('proxy_not_ready', 503, 'proxy')
                from uuid import UUID
                loader = getattr(self.profiles, 'load', self.profiles)
                selection = await loader(UUID(stamp['proxy_id']))
        return selection, stamp

    async def resolve(self, credential_id):
        selection, _ = await self.policy(credential_id)
        return selection
