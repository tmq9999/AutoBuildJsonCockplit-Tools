# Final bounded-admission acceptance repair fix wave

Date: 2026-09-27
Branch: `feature/bounded-admission-capacity`

## Scope

This report records the single post-review fix wave for Important findings 1–5. The provider limit remains 16, proxy endpoint/key exclusivity remains unchanged, direct fallback remains disabled, and OAuth/model/quota/pricing/redaction paths were not changed.

## Fixes

- Admission now exposes bounded resource-wait episodes with provider/proxy waiting depth, terminal success/expiry/cancellation counters, and one uncapped elapsed sample per episode. Provider polling attempts remain separate counters; provider sample history remains bounded.
- Serving telemetry publishes on provider/proxy and preparation-cleanup transitions and refreshes idle snapshots on a 10-second heartbeat (below the 30-second stale threshold), retaining one coalescing writer.
- Provider acquisition and PostgreSQL lease claims use wall-clock remaining-deadline bounds around the full transaction, including pool checkout and commit. Cleanup uses an independent 250ms wall-clock bound and cancellation-safe owned tasks. Expired lease claims map to redacted `deadline_exceeded`.
- Proxy store health/claim calls are bounded by the remaining 10-second acquisition window while lease hard deadlines still use the request deadline, so provider capacity is released before retry waits.
- Migration contract now expects `0016_capacity_snapshots` and verifies the new table while preserving existing rows.

## Fresh failing tests (RED)

```text
../../.venv/bin/python -m pytest tests/gateway/unit/test_admission.py::test_resource_wait_episode_records_total_elapsed_and_terminal_state tests/gateway/unit/test_admission.py::test_resource_wait_cancel_and_expiry_are_counted_once_with_bounded_history tests/gateway/unit/test_capacity_publisher.py::test_capacity_publisher_refreshes_idle_snapshot_with_heartbeat -q
2 failed, 1 passed
```

The new PostgreSQL regressions initially failed on uncapped pool checkout/cleanup, `ValueError('invalid_deadline')`, and 50ms provider poll samples; these failures were observed before production changes.

## Verification

Environment for PostgreSQL-capable runs:

```text
AUTOBUILD_TEST_POSTGRES_BIN=.deps/gateway-pg18/root/usr/lib/postgresql/18/bin
LD_LIBRARY_PATH=.deps/gateway-pg18/root/usr/lib/x86_64-linux-gnu:.deps/gateway-pg18/root/usr/lib/postgresql/18/lib
```

Focused final command:

```text
/home/mquangprovip0503/Work/Code/AutoBuildJsonCockplit-Tools/.venv/bin/python -m pytest tests/gateway/unit/test_admission.py tests/gateway/unit/test_capacity_publisher.py tests/gateway/unit/test_engine_resource_order.py tests/gateway/unit/test_proxy_manager.py tests/gateway/integration/test_final_capacity_regressions.py tests/gateway/integration/test_provider_admission_wait.py tests/gateway/integration/test_proxy_leases.py tests/gateway/integration/test_catalog_schema.py::test_catalog_upgrade_from_revision_nine_preserves_existing_rows tests/gateway/integration/test_codex_service_status.py -q --tb=short
65 passed in 16.75s
```

Targeted unit command:

```text
/home/mquangprovip0503/Work/Code/AutoBuildJsonCockplit-Tools/.venv/bin/python -m pytest tests/gateway/unit/test_admission.py tests/gateway/unit/test_capacity_publisher.py tests/gateway/unit/test_engine_resource_order.py tests/gateway/unit/test_proxy_manager.py -q
34 passed in 0.76s
```

Quality checks:

```text
/home/mquangprovip0503/Work/Code/AutoBuildJsonCockplit-Tools/.venv/bin/ruff check <changed production/tests files>
All checks passed!
/home/mquangprovip0503/Work/Code/AutoBuildJsonCockplit-Tools/.venv/bin/python -m compileall -q autobuild_json
exit 0
git diff --check
exit 0
```

Additional PostgreSQL-capable unit sweep:

```text
/home/mquangprovip0503/Work/Code/AutoBuildJsonCockplit-Tools/.venv/bin/python -m pytest tests/gateway/unit -q --tb=no
768 passed in 28.66s
```

The complete repository suite was not re-run in this fix-wave report because the worktree dependency fixture required the separate pinned dependency symlink and the controller was resolving that environment. No live restart, burst, or service mutation was performed.

## Unresolved concerns

- Final live acceptance evidence (restart, real stream/non-stream requests, 100-user burst, usage reconciliation, and cleanup evidence) remains a separate acceptance step and is intentionally not claimed here.
- PostgreSQL status tests pass with the bundled PG18 environment above; the default worktree PG17 path is unavailable without the controller-provided dependency/runtime setup.
