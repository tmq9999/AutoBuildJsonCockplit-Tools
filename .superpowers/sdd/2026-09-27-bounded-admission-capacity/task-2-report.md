# Task 2 report: deadline-aware provider admission waiting

## Implementation

- Extended `ProviderLimits.acquire(route, deadline, *, wait=False)` without changing the default for existing callers.
- Added short, committed admission attempts. Provider and credential rows are locked only during the check/insert transaction; no SQLAlchemy session is held while a waiter sleeps.
- `wait=True` retries only active-concurrency exhaustion. Disabled/missing records, cooldown, RPM exhaustion, and other existing provider errors remain immediate.
- Added process-local `asyncio.Condition` release notification with a bounded 50 ms fallback poll for releases from another `ProviderLimits` instance/process.
- Added deadline expiry handling and private wait/expired counters, plus a redacted `snapshot()` for later metrics wiring.
- Release updates notify waiters from the `finally` path. No `Engine` code was modified.

## TDD evidence

The mandated RED run was:

```text
pytest tests/gateway/integration/test_provider_admission_wait.py -q
5 failed, 1 passed
```

The failures were the expected unsupported `wait=True` calls. The one passing test was the existing non-waiting regression.

## Verification

With PostgreSQL 18 configured via `AUTOBUILD_TEST_POSTGRES_BIN` and `LD_LIBRARY_PATH`:

```text
pytest tests/gateway/integration/test_provider_admission_wait.py -q
6 passed in 3.35s

pytest tests/gateway/integration/test_provider_admission_wait.py tests/gateway/integration/test_review_regressions.py -q
15 passed in 7.13s

pytest tests/gateway/integration/test_cooldown.py tests/gateway/integration/test_rate_limits.py tests/gateway/integration/test_provider_probe.py -q
79 passed in 34.80s
```

The external virtualenv emitted the existing `sys.prefix`/`sys.exec_prefix` runtime warnings because it is referenced from the worktree; PostgreSQL and all tests completed successfully. No library dependency issue blocked verification.
