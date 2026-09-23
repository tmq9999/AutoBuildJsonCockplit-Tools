import threading
import pytest
from autobuild_json.runner import Runner
from autobuild_json.results import RunStore
from autobuild_json.input_parser import parse_accounts
from autobuild_json.proxies import parse_proxies
from autobuild_json.errors import FlowError
from tests.fakes import success_result


def accounts(count):
    return parse_accounts("\n".join(f"u{i}@example.com|p|JBSWY3DPEHPK3PXP" for i in range(count)))


def test_stop_and_active_batch_conflict(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def processor(account, proxy, context):
        entered.set()
        assert release.wait(2)
        context.check()
        return success_result(account.email)
    runner = Runner(RunStore(tmp_path), processor)
    job = runner.start(accounts(3), [], "sequential", 1, 180)
    assert entered.wait(2)
    with pytest.raises(FlowError) as error:
        runner.start(accounts(1), [], "sequential", 1, 180)
    assert error.value.code == "BATCH_CONFLICT"
    runner.stop(job)
    release.set()
    runner.wait(job, 3)
    assert runner.get(job)["counts"]["cancelled"] == 3
    assert runner.get(job)["status"] == "cancelled"
    runner.close()


def test_concurrency_proxy_and_email_exclusion(tmp_path):
    lock = threading.Lock()
    active, emails, proxies = set(), set(), set()
    maximum = [0]
    def processor(account, proxy, context):
        with lock:
            assert account.email not in emails
            assert proxy.key not in proxies
            active.add(account.account_job_id)
            emails.add(account.email)
            proxies.add(proxy.key)
            maximum[0] = max(maximum[0], len(active))
        context.cancel.wait(.02)
        with lock:
            active.remove(account.account_job_id)
            emails.remove(account.email)
            proxies.remove(proxy.key)
        return success_result(account.email)
    report = parse_accounts("\n".join(f"u{i%3}@example.com|p|JBSWY3DPEHPK3PXP" for i in range(12)))
    runner = Runner(RunStore(tmp_path), processor)
    job = runner.start(report, parse_proxies("host1:8080\nhost2:8080"), "parallel", 6, 180)
    runner.wait(job, 4)
    assert maximum[0] == 2
    assert runner.get(job)["counts"]["success"] == 12
    runner.close()


def test_round_robin_even_in_sequential_mode(tmp_path):
    assigned = []
    def processor(account, proxy, context):
        assigned.append(proxy.server)
        return success_result(account.email)
    runner = Runner(RunStore(tmp_path), processor)
    job = runner.start(accounts(4), parse_proxies("one:8\ntwo:8"), "sequential", 8, 180)
    runner.wait(job, 3)
    assert assigned == ["http://one:8", "http://two:8"] * 2
    runner.close()


def test_disk_failure_stops_and_does_not_claim_success(tmp_path, monkeypatch):
    store = RunStore(tmp_path)
    original = store.update
    processed = []
    def update(*args, **kwargs):
        if kwargs.get("status") == "success":
            raise FlowError("STORAGE_ERROR", "storage")
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "update", update)
    def processor(account, proxy, context):
        processed.append(account.email)
        return success_result(account.email)
    runner = Runner(store, processor)
    job = runner.start(accounts(3), [], "sequential", 1, 180)
    runner.wait(job, 3)
    snapshot = runner.get(job)
    assert len(processed) == 1
    assert snapshot["status"] == "error"
    assert snapshot["batch_error"]["code"] == "STORAGE_ERROR"
    assert snapshot["counts"]["success"] == 0
    assert snapshot["counts"]["running"] == 0
    assert snapshot["counts"]["queued"] == 0
    assert snapshot["counts"]["error"] == 1
    assert snapshot["counts"]["cancelled"] == 2
    assert store.list_runs()[0]["status"] == "error"
    runner.close()


def test_total_storage_failure_gives_consistent_history_and_exports(tmp_path, monkeypatch):
    import json
    store = RunStore(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def processor(account, proxy, context):
        entered.set()
        assert release.wait(2)
        return success_result(account.email)
    runner = Runner(store, processor)
    job = runner.start(accounts(2), [], "sequential", 1, 180)
    assert entered.wait(2)
    def disk_full(*args, **kwargs):
        raise FlowError("STORAGE_ERROR", "storage")
    monkeypatch.setattr(store, "_save", disk_full)
    release.set()
    runner.wait(job, 3)
    assert store.list_runs()[0]["status"] == runner.get(job)["status"] == "error"
    assert len(json.loads(store.export(job,"errors"))) == 1
    assert len(json.loads(store.export(job,"cancelled"))) == 1
    assert json.loads(store.export(job,"success")) == []
    runner.close()


def test_deadline_and_exception_redaction(tmp_path):
    def processor(account, proxy, context):
        raise RuntimeError("private-pass")
    runner = Runner(RunStore(tmp_path), processor)
    job = runner.start(accounts(1), [], "sequential", 1, 180)
    runner.wait(job, 3)
    assert runner.get(job)["counts"]["error"] == 1
    assert "private-pass" not in repr(runner.get(job))
    runner.close()
