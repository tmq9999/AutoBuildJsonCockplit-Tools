"""One async proxy-control loop per legacy batch, without changing HTTP auth code."""
import asyncio
from concurrent.futures import CancelledError
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import threading
import time
from uuid import UUID

from ...errors import FlowError
from ..errors import GatewayError
from .config import ProxySelection, KiotKey


@dataclass(frozen=True)
class SourceSlot:
    key: str
    selection: ProxySelection


class LegacyProxySource:
    def __init__(self, selection, manager):
        self.selection, self.manager = selection, manager
        self.slots = []
        if selection.mode != "direct":
            for entry in selection.runtime_entries:
                key = entry.fingerprint if isinstance(entry, KiotKey) else entry.key
                self.slots.append(SourceSlot(key, ProxySelection(selection.mode if selection.mode == "kiotproxy" else "fixed",
                    runtime_entries=(entry,), region=selection.region, protocol=selection.protocol, rotate=selection.rotate)))
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="oauth-proxy-control")
        self.thread.start()

    async def _execute(self, account, slot, context, processor, timeout):
        selection = slot.selection if slot else self.selection
        hard_deadline = datetime.now(timezone.utc)+timedelta(seconds=min(600, timeout+10))
        owner = UUID(account.account_job_id)
        parent=asyncio.current_task()
        async def watch():
            while not context.cancel.is_set():
                await asyncio.sleep(.05)
            parent.cancel()
        watcher=asyncio.create_task(watch())
        try:
            async with self.manager.acquire(selection,owner,hard_deadline) as lease:
                context.deadline=time.monotonic()+min(timeout,(hard_deadline-datetime.now(timezone.utc)).total_seconds())
                context.check()
                operation=asyncio.create_task(asyncio.to_thread(processor,account,lease.proxy,context))
                try:
                    return await asyncio.shield(operation)
                except asyncio.CancelledError:
                    context.cancel.set()
                    while not operation.done():
                        try:
                            await asyncio.shield(operation)
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            break
                    if operation.done() and not operation.cancelled():
                        operation.exception()
                    raise
        except asyncio.CancelledError:
            context.cancel.set()
            raise FlowError("CANCELLED", "proxy_acquire") from None
        finally:
            watcher.cancel()
            await asyncio.gather(watcher,return_exceptions=True)

    def execute(self, account, slot, context, processor, timeout):
        context.on_stage("proxy_acquire", 1)
        try:
            return asyncio.run_coroutine_threadsafe(self._execute(account, slot, context, processor, timeout), self.loop).result()
        except (CancelledError, asyncio.CancelledError):
            raise FlowError("CANCELLED", "proxy_acquire") from None
        except GatewayError:
            raise FlowError("PROXY_ERROR", "proxy_acquire") from None

    def close(self):
        if self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
        if not self.thread.is_alive():
            self.loop.close()
