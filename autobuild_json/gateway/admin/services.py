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
        self.catalog_worker = None
        self._loop = None
        self._thread = None

    def build_catalog_worker(self):
        """Compose the maintenance worker without starting network/background work."""
        if self.catalog_worker is not None:
            return self.catalog_worker
        from ..catalog.sources import SourceRepository
        from ..catalog.operations import OperationStore
        from ..catalog.snapshots import SnapshotStore
        from ..catalog.runner import OperationRunner
        from ..catalog.worker import CatalogWorker
        from ..catalog.scheduler import Scheduler
        from ..providers.discovery.client import DiscoveryClient
        from ..providers.limits import ProviderLimits
        sources = SourceRepository(self.db, self.vault, self.pepper)
        operations = OperationStore(self.db, sources)
        snapshots = SnapshotStore(self.db, sources, operations,
                                   self.vault.derive_key("client_keys", "catalog-cursor-v1"))
        discovery = DiscoveryClient(self.transport, ProviderLimits(self.db), self.engine.credential)
        runner = OperationRunner(sources, operations, snapshots, discovery, self.proxies,
                                 self.profiles, self.catalog)
        self.catalog_operations, self.catalog_sources = operations, sources
        self.catalog_worker = CatalogWorker(self.db, runner, Scheduler(self.db, operations, sources))
        return self.catalog_worker

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
