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
        from ..accounts.quota import CodexQuotaService
        from ..accounts.quota_store import QuotaStore
        self.quota_store = QuotaStore(db)
        self.codex_quota = CodexQuotaService(db, self.engine.credentials, self.proxies,
                                           self.profiles, self.transport, self.quota_store)
        self.catalog_worker = None
        self.sources = self.operations = self.snapshots = self.decisions = None
        self.publisher = self.quotes = self.probe_accounting = None
        self.discovery_client = self.operation_runner = None
        self._loop = None
        self._thread = None
        self.build_catalog_worker()

    def build_catalog_worker(self):
        """Compose the maintenance worker without starting network/background work."""
        if self.catalog_worker is not None:
            # Compatibility callers may replace transport after construction.
            self.discovery_client.transport = self.transport
            return self.catalog_worker
        from ..catalog.sources import SourceRepository
        from ..catalog.operations import OperationStore
        from ..catalog.snapshots import SnapshotStore
        from ..catalog.runner import OperationRunner
        from ..catalog.worker import CatalogWorker
        from ..catalog.scheduler import Scheduler
        from ..providers.discovery.client import DiscoveryClient
        from ..providers.limits import ProviderLimits
        from ..catalog.probe import ProbeService
        from ..catalog.probe_quote import ProbeQuotes
        from ..catalog.probe_accounting import ProbeAccounting
        from ..catalog.decisions import DecisionStore
        from ..catalog.publish import Publisher
        from ..catalog.retention import Retention
        sources = SourceRepository(self.db, self.vault, self.pepper)
        operations = OperationStore(self.db, sources)
        cursor_key = self.vault.derive_key("client_keys", "catalog-cursor-v1")
        snapshots = SnapshotStore(self.db, sources, operations, cursor_key)
        limits = ProviderLimits(self.db)
        discovery = DiscoveryClient(self.transport, limits, self.engine.credential)
        quotes = ProbeQuotes(self.db, sources)
        accounting = ProbeAccounting(self.db, operations, self.budgets)
        probe = ProbeService(self.db, sources, operations, quotes, accounting, self.engine.adapters,
                             self.transport, limits, self.proxies, self.profiles, self.catalog)
        runner = OperationRunner(sources, operations, snapshots, discovery, self.proxies,
                                 self.profiles, self.catalog, probe=probe)
        self.catalog_operations, self.catalog_sources = operations, sources
        self.sources, self.operations, self.snapshots = sources, operations, snapshots
        self.decisions, self.publisher, self.quotes = DecisionStore(self.db, sources), Publisher(self.db, sources), quotes
        self.probe_accounting, self.discovery_client, self.operation_runner = accounting, discovery, runner
        self.retention = Retention(self.db)
        self.catalog_worker = CatalogWorker(self.db, runner, Scheduler(self.db, operations, sources), probe)
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
