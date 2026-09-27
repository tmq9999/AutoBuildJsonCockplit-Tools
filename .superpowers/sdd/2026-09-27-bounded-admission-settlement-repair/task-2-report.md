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
