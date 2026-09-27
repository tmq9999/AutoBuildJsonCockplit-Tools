# Task 2 implementation report

## Scope

Interrupted settlement is now explicit and recoverable. `PreparedCall.close()`
continues to join one owned cleanup task, writes the interrupted marker only
after dispatch, and invokes one idempotent ledger reconciliation path. The
ledger settles only a saved, structurally valid authoritative attempt receipt;
unknown usage remains `usage_pending` with its hold intact. Maintenance uses the
same reconciliation path and exposes a redacted pending count, recoverable
count, oldest pending age, and last recovery outcome through `snapshot()`.

## Changed files

- `autobuild_json/gateway/engine.py`
  - Cleanup marks interruption and reconciles the same attempt while preserving
    idempotent close/generator-finalization behavior and resource release.
- `autobuild_json/gateway/metering/ledger.py`
  - Added `reconcile_interrupted()` and redacted `status_snapshot()`.
  - Authoritative usage is validated and settled exactly once under the request
    lock; no bounds/elapsed-time/fabricated usage is used.
- `autobuild_json/gateway/maintenance.py`
  - Recovery delegates to ledger reconciliation; added the redacted durable
    maintenance snapshot.
- `autobuild_json/gateway/admin/codex_accounts.py`
  - The private service-status surface now includes the durable redacted
    settlement snapshot.
- `tests/gateway/integration/test_recovery.py`
  - Added pending snapshot redaction/age and authoritative recovery outcome
    coverage.
- `tests/gateway/unit/test_engine_admission.py`
  - Added concurrent double-close coverage proving one reconciliation call and
    one admission release.

## TDD / focused test evidence

The new tests were first run against the unmodified behavior and failed:

- `test_recovery_snapshot_redacts_pending_age_and_outcome`: `AttributeError`,
  no `Maintenance.snapshot()` existed.
- `test_recovery_uses_authoritative_attempt_and_records_outcome_once`:
  `AttributeError`, no `Maintenance.snapshot()` existed.
- `test_prepared_close_joins_one_recovery_cleanup_and_settles_saved_usage`:
  `AttributeError`, cleanup had no reconciliation path.

After implementation, focused PostgreSQL tests were run with the bundled
PostgreSQL 18 binary (`AUTOBUILD_TEST_POSTGRES_BIN` and `LD_LIBRARY_PATH` set):

```text
pytest tests/gateway/integration/test_recovery.py tests/gateway/unit/test_engine_admission.py -q
13 passed in 4.89s

pytest tests/gateway/integration/test_stream_lifecycle.py \
       tests/gateway/integration/test_recovery.py \
       tests/gateway/unit/test_engine_admission.py -q
20 passed in 7.19s

pytest tests/gateway/integration/test_gateway.py \
       tests/gateway/integration/test_recovery.py \
       tests/gateway/integration/test_codex_service_status.py -q
22 passed in 10.68s

pytest tests/gateway/integration/test_ledger.py \
       tests/gateway/integration/test_codex_quota.py -q
42 passed in 14.63s

pytest tests/gateway/unit/test_engine_resource_order.py \
       tests/gateway/unit/test_admission.py \
       tests/gateway/unit/test_catalog_worker.py -q
27 passed in 5.65s

Final combined focused run of all recovery, gateway stream, private status,
ledger/quota, admission, resource-order, and maintenance-worker selections:
105 passed in 28.47s
```

These cases assert zero active provider admissions and zero owned proxy leases
where applicable (existing disconnect/cleanup and quota regression assertions),
and assert zero held quota after settled/released cases. Unknown-disconnect and
unknown-recovery cases explicitly assert the hold remains nonzero and no usage
ledger settlement row is created. Authoritative recovery asserts bucket state
`spent=15, held=0` after two maintenance ticks, proving one settlement.
The real PostgreSQL disconnect/late-receipt regression also asserts the initial
pending request has no settlement row, both resource-owner counts are zero,
then two recovery ticks leave exactly one settlement row, zero held quota, and
both resource-owner counts still zero.

The complete gateway suite was also run once with the bundled PostgreSQL
environment:

```text
pytest tests/gateway -q
6 failed, 1703 passed, 1 warning in 400.25s (0:06:40)
```

All six failures are the Task 1 retry expectation in
`test_codex_egress.py::test_configured_proxy_connection_failure_never_falls_back_direct`
across JSON/SSE and fixed/pool/Kiot modes: the test expects one connection but
Task 1 intentionally permits one safe pre-response proxy retry and makes two.
No Task 2 settlement/recovery test failed. Task 2 does not change Task 1 retry
behavior or update that out-of-scope expectation.

## Static checks

```text
ruff check autobuild_json/gateway/engine.py autobuild_json/gateway/maintenance.py \
  autobuild_json/gateway/metering/ledger.py tests/gateway/integration/test_recovery.py \
  tests/gateway/unit/test_engine_admission.py
All checks passed!

python -m compileall -q autobuild_json/gateway/engine.py \
  autobuild_json/gateway/maintenance.py autobuild_json/gateway/metering/ledger.py \
  tests/gateway/integration/test_recovery.py tests/gateway/unit/test_engine_admission.py
Passed (no output).
```

## Environment / concerns

No environment limitations remained after using the bundled PostgreSQL
environment and repository virtualenv. The six full-suite failures are the
pre-existing Task 1 retry/test-expectation mismatch described above. Unknown
provider usage intentionally remains pending and is not automatically released
or inferred.

`oldest_pending_age_seconds` is defined as time since admission of the oldest
currently pending request; this needs no schema migration and is conservative
for operators. `recovery_outcome` is reconstructed from durable request and
attempt rows on each status read (`clear`, `authoritative_usage_available`, or
`unknown_usage_pending`), so it survives maintenance-worker or admin-process
restart and never contains identifiers or error strings.

## Review fix round

Addressed all review findings in the amended follow-up commit:

- `PreparedCall` bounded every accounting/provider/stack/admission cleanup child,
  cancels and boundedly joins timed-out children, shares one owned cleanup task
  and final exception across concurrent callers, and leaves `closed=False` when
  cleanup ownership could not be joined. Regression proves bounded return,
  no detached child tasks, and no false closed claim.
- Ledger snapshot validation now calls the same `Usage` structural validator as
  reconciliation. A malformed completed-attempt receipt is not counted as
  recoverable and remains pending without a usage-ledger row.
- Prepare/count-token cleanup now reads durable request state after cancellation;
  a committed dispatch is reconciled as pending, while a genuinely reserved
  request still uses `not_dispatched`. PostgreSQL regression covers the commit-
  then-cancel boundary.
- Task 1 proxy retry expectations were updated in the existing Codex egress
  regression: exactly two configured-proxy connections and two attempts are
  expected; direct fallback remains forbidden.

Review-focused matrix:

```text
129 passed in 41.28s
```

Full gateway suite after the fix:

```text
1712 passed, 1 warning in 443.38s (0:07:23)
```

## Review fix round 2

The cleanup lifecycle now uses one persistent `PreparedCall` supervisor with a
single monotonic grace period shared by accounting and resource cleanup. At the
deadline it cancels unfinished children once, remains their owner while they
unwind, and keeps `closed=False`. A caller receives bounded `TimeoutError`, but
later `close()` calls rejoin that same supervisor and drain cancellation-
resistant work; no child is abandoned or replayed through a failed shell.
Original non-timeout cleanup errors remain the terminal error. Provider/proxy/
stream and admission cleanup run independently of accounting so each is
attempted safely.

The new controlled cancellation-resistant unit regression proves ownership is
retained after the first timeout and empty after a later close drains the same
supervisor. Existing stream lifecycle integration remains green, including
disconnect cancellation and concurrent close joining.

The private status read now reuses its existing repeatable-read database
session rather than opening a nested connection. Settlement rows are streamed
and validated incrementally, avoiding an unbounded in-memory `.all()` over
historical pending requests. `oldest_pending_age_seconds` remains age since
admission, and `recovery_outcome` is the current durable aggregate—not a
historical last-operation value.

```text
pytest tests/gateway/unit/test_engine_admission.py -q
9 passed in 0.51s

pytest tests/gateway/unit/test_engine_admission.py \
  tests/gateway/integration/test_recovery.py \
  tests/gateway/integration/test_gateway.py \
  tests/gateway/integration/test_stream_lifecycle.py \
  tests/gateway/integration/test_codex_service_status.py -q
40 passed in 14.92s

ruff check <round-2 changed Python files>
All checks passed!

python -m compileall -q <round-2 changed Python files>
Passed (no output).

git diff --check
Passed (no output).
```

An additional full-suite run was stopped after the controller confirmed the
targeted matrix was sufficient; it had reached 188 passed with no failures in
82.48s before interruption. The prior completed full-suite gate remains 1712
passed. No production database was mutated. Operational observation supplied
by the controller: the local runner does not launch maintenance, and historical
interrupted rows will be handled by explicit Task 4 recovery verification.

## Review fix round 3

Split stream cancellation, route/proxy stack close, and admission-stack close
into independently owned supervisor children. Normal cleanup preserves
stream/route-before-admission ordering. If either resource child is resistant,
the admission child waits only until a small reserve inside the same monotonic
cleanup budget, then releases admission independently before the caller's
bounded timeout. The supervisor retains all unfinished children for later
drain; it never marks the call closed or abandons a detached task. A
permanently resistant dependency therefore leaves `closed=False` until a later
close/shutdown drain can join it.

```text
pytest tests/gateway/unit/test_engine_admission.py -q
11 passed in 0.49s

pytest tests/gateway/unit/test_engine_admission.py \
  tests/gateway/integration/test_recovery.py \
  tests/gateway/integration/test_gateway.py \
  tests/gateway/integration/test_stream_lifecycle.py \
  tests/gateway/integration/test_codex_service_status.py -q
42 passed in 12.81s

ruff check <changed Python files>
All checks passed!

python -m compileall -q <changed Python files>
Passed (no output).

git diff --check
Passed (no output).
```

## Review fix round 4

Addressed the resource ownership finding by making `PreparedCall` resource
cleanup one sequential supervisor child: provider stream cancellation completes
before the route/adapter `AsyncExitStack` closes, and admission closes last.
This prevents `ProviderStream.cancel()` and transport context teardown from
concurrently calling the same raw response close or releasing proxy/provider
ownership early. A cancellation-resistant stream therefore retains the gate,
provider admission, and proxy lease until its owned supervisor drains.

Prepared cleanup supervisors are now registered on the engine and drained by
the application lifecycle. Request-task cancellation or a bounded caller
timeout cannot abandon the route owner; lifecycle drain retries the same task
and leaves `closed=False` until all resources complete. Accounting remains an
independent idempotent child and no usage is inferred.

The realistic PostgreSQL stream regression delays the actual transport raw
response close while exercising the real provider admission, proxy lease,
adapter, and route stack. It proves no concurrent response closes, ownership
remains active after both normal timeout and caller cancellation, and lifecycle
drain releases all resources with the request still `usage_pending` and no
ledger settlement.

```text
pytest tests/gateway/unit/test_engine_admission.py \
  tests/gateway/integration/test_stream_lifecycle.py \
  tests/gateway/integration/test_recovery.py \
  tests/gateway/integration/test_gateway.py \
  tests/gateway/integration/test_codex_service_status.py -q
44 passed in 12.08s

ruff check autobuild_json/gateway/engine.py autobuild_json/gateway/http/app.py \
  tests/gateway/unit/test_engine_admission.py tests/gateway/integration/test_stream_lifecycle.py
All checks passed!

git diff --check
Passed (no output).
```

No production database was mutated. The untracked data symlink and handoff
document remain untouched.
