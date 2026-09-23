import asyncio
import threading

from ..identity.service import IdentityService
from ..providers.catalog import Catalog
from ..metering.ledger import Ledger
from ..metering.budgets import BudgetService
from ..proxy.profiles import ProfileStore
from ..proxy.pg_leases import PgLeaseStore
from ..proxy.manager import ProxyManager
from ..transport.egress import EgressPolicy
from ..transport.http import Transport
from ..engine import Engine


class AdminServices:
    def __init__(self, db, vault, pepper, *, egress=None, transport=None):
        self.db, self.vault, self.pepper = db, vault, pepper
        self.identity, self.catalog = IdentityService(db, pepper), Catalog(db, vault)
        self.ledger, self.budgets = Ledger(db), BudgetService(db)
        self.profiles = ProfileStore(db, vault, pepper)
        self.proxies = ProxyManager(PgLeaseStore(db))
        self.transport = transport or Transport(egress or EgressPolicy())
        self.engine = Engine(db, self.catalog, self.ledger, self.proxies, self.transport, vault,
                             digest_key=pepper, proxy_resolver=self.profiles.load)
        self._loop = None
        self._thread = None

    def load_proxy_sync(self, identity):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
            self._thread.start()
        return asyncio.run_coroutine_threadsafe(self.profiles.load(identity), self._loop).result()

    async def close(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self._loop.close()
        await self.db.close()
