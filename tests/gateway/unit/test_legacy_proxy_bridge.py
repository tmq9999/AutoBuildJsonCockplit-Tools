import time
from dataclasses import dataclass

import httpx
import pytest
from pydantic import ValidationError


def test_static_payload_remains_accepted_and_conflicts_are_rejected():
    from autobuild_json.api_models import JobInput
    item = JobInput(accounts_text="a@example.com|p|JBSWY3DPEHPK3PXP", proxies_text="http://proxy.invalid:8080")
    assert item.proxy_mode == "legacy"
    with pytest.raises(ValidationError):
        JobInput(accounts_text="x", proxy_mode="direct", proxies_text="proxy.invalid:8080")
    with pytest.raises(ValidationError):
        JobInput(accounts_text="x", proxy_mode="kiotproxy")


@pytest.mark.parametrize("missing_current", [False, True])
def test_runner_resolves_kiot_slot_and_preserves_legacy_result(tmp_path, missing_current):
    from autobuild_json.gateway.proxy.legacy import LegacyProxySource
    from autobuild_json.gateway.proxy.manager import ProxyManager
    from autobuild_json.gateway.proxy.leases import MemoryLeaseStore
    from autobuild_json.gateway.proxy.kiot import KiotClient
    from autobuild_json.gateway.proxy.config import ProxySelection, parse_kiot_keys
    from autobuild_json.runner import Runner
    from autobuild_json.results import RunStore
    from autobuild_json.input_parser import parse_accounts
    from tests.fakes import success_result
    from .test_kiot import success
    calls = []
    def respond(request):
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if missing_current and calls == ["current"]:
            return httpx.Response(400, json={"success": False, "error": "PROXY_NOT_FOUND_BY_KEY"})
        return httpx.Response(200, json=success())
    source = LegacyProxySource(ProxySelection("kiotproxy", runtime_entries=tuple(parse_kiot_keys("synthetic", pepper=b"p"*32))),
        ProxyManager(MemoryLeaseStore(), KiotClient(adapter=httpx.MockTransport(respond))))
    used = []
    def processor(account, proxy, context):
        used.append(proxy.server)
        assert context.remaining_timeout() > 0
        return success_result(account.email)
    runner = Runner(RunStore(tmp_path), processor)
    report = parse_accounts("a@example.com|p|JBSWY3DPEHPK3PXP\nb@example.com|p|JBSWY3DPEHPK3PXP")
    job = runner.start(report, [], "parallel", 2, 10, proxy_source=source)
    runner.wait(job, 10)
    assert used == ["http://93.184.216.34:39008"] * 2
    assert calls == (["current", "new", "current"] if missing_current else ["current", "current"])
    assert runner.get(job)["status"] == "completed"
    runner.close()


def test_stop_cancels_wait_without_starting_account(tmp_path):
    import asyncio
    from contextlib import asynccontextmanager
    from autobuild_json.gateway.proxy.legacy import LegacyProxySource
    from autobuild_json.gateway.proxy.config import ProxySelection
    from autobuild_json.models import ProxyConfig
    from autobuild_json.runner import Runner
    from autobuild_json.results import RunStore
    from autobuild_json.input_parser import parse_accounts
    @dataclass
    class WaitingManager:
        @asynccontextmanager
        async def acquire(self, *args):
            await asyncio.sleep(10)
            yield None
    source = LegacyProxySource(ProxySelection("fixed", runtime_entries=(ProxyConfig("http://proxy.invalid:80"),)), WaitingManager())
    started = []
    runner = Runner(RunStore(tmp_path), lambda *args: started.append(True))
    job = runner.start(parse_accounts("a@example.com|p|JBSWY3DPEHPK3PXP"), [], "sequential", 1, 30, proxy_source=source)
    time.sleep(0.03)
    runner.stop(job)
    runner.wait(job, 2)
    assert not started
    runner.close()
