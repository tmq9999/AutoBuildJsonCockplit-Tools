import json
import os
from concurrent.futures import ThreadPoolExecutor
import pytest
from autobuild_json.results import RunStore
from autobuild_json.input_parser import parse_accounts
from autobuild_json.errors import FlowError
from tests.fakes import success_result


@pytest.fixture
def report():
    return parse_accounts("u@example.com|private-pass|JBSWY3DPEHPK3PXP\nv@example.com|p|JBSWY3DPEHPK3PXP\nbad|secret|")


def test_exact_export_private_snapshot_and_restart(tmp_path, report):
    store = RunStore(tmp_path)
    job = store.create(report)
    store.update(job, report.accounts[0].account_job_id, status="success", stage="complete", attempt=1, result=success_result())
    store.update(job, report.accounts[1].account_job_id, status="running", stage="password", attempt=1)
    reopened = RunStore(tmp_path)
    reopened.recover_interrupted()
    row = json.loads(reopened.export(job, "success"))[0]
    assert set(row) == {"id", "email", "account", "tokens"}
    assert set(row["tokens"]) == {"id_token", "access_token", "refresh_token"}
    snapshot = reopened.snapshot(job)
    assert snapshot["counts"]["success"] == 1
    assert snapshot["counts"]["cancelled"] == 1
    assert "fake-refresh" not in repr(snapshot)
    disk = (tmp_path / job / "run.json").read_text()
    assert "private-pass" not in disk
    assert "JBSWY3DPEHPK3PXP" not in disk
    assert json.loads(reopened.export(job, "cancelled"))[0]["reason"] == "Interrupted by application restart"
    if os.name == "posix":
        assert (tmp_path / job / "run.json").stat().st_mode & 0o777 == 0o600


def test_disk_failure_never_reports_success(tmp_path, report, monkeypatch):
    store = RunStore(tmp_path)
    job = store.create(report)
    def fail(*args):
        raise OSError("disk full private-pass")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(FlowError) as error:
        store.update(job, report.accounts[0].account_job_id, status="success", stage="done", attempt=1, result=success_result())
    assert error.value.code == "STORAGE_ERROR"
    assert store.snapshot(job)["counts"]["success"] == 0
    assert json.loads(store.export(job, "success")) == []


def test_export_path_guards_and_empty(tmp_path, report):
    store = RunStore(tmp_path)
    job = store.create(report)
    assert json.loads(store.export(job, "success")) == []
    for identity, kind in [("../x", "success"), (job, "../../secret"), ("not-found", "success")]:
        with pytest.raises(FlowError):
            store.export(identity, kind)


def test_concurrent_updates_not_lost(tmp_path):
    report = parse_accounts("\n".join(f"u{i}@example.com|p|JBSWY3DPEHPK3PXP" for i in range(20)))
    store = RunStore(tmp_path)
    job = store.create(report)
    def update(account):
        store.update(job, account.account_job_id, status="success", stage="done", attempt=1, result=success_result(account.email))
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(update, report.accounts))
    assert store.snapshot(job)["counts"]["success"] == 20
