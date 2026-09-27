# Provider admission cleanup regression report

Date: 2026-09-27 (Asia/Bangkok)

## Finding

`ProviderLimits._release_admission()` used one 250 ms `wait_for()` around a
database checkout and update. `ProviderLimits.acquire()` awaited that cleanup
from its context manager `finally`, so a pool checkout timeout raised
`deadline_exceeded` after request settlement and could replace the real
response. The live database contained two fresh active admission rows whose
request rows were already completed, alongside two older pre-existing rows.

The live inspection was read-only. It used the configured local PostgreSQL
listener and the password file already used by the local gateway; no password,
request body, API key, or raw upstream response is recorded here.

## Evidence

At 2026-09-27 19:35:39 +07, read-only SQL reported:

- Four active provider admission rows total; two had admission times around
  19:28:35 and 19:28:50 +07 and deadlines around 19:31:25 +07.
- The two fresh rows were older than their deadlines and remained `active`.
- In the last 20 minutes, 96 request rows were `completed`, 6 were `released`,
  and 102 rows existed in total.
- `pg_stat_activity` showed 18 idle sessions and one active inspection session;
  there were no granted locks held by another backend at inspection time.

This snapshot cannot reconstruct the completed burst's historical pool
contention, but it is consistent with cleanup timing out while the request
itself had already settled. The deterministic PostgreSQL regression below
reproduces the relevant boundary with all three connections in a pool of size
three held by concurrent work.

## Fix

`ProviderLimits` now retries admission cleanup twice within a 0.5 second
bounded window. A failed cleanup is counted in the provider snapshot as
`cleanup_failures`; it is not silently converted into a successful release.
The context manager waits for and joins the owned cleanup task, preserves the
request's primary result/error, and still notifies local waiters. Direct
`_release_admission()` calls retain their existing bounded error behavior.

## Regression test

`test_provider_cleanup_retries_after_transient_pool_contention` acquires a
real provider admission using PostgreSQL, occupies every connection in a
separate pool, starts admission cleanup, then frees one connection after the
first 250 ms attempt expires. Before the fix it raised
`GatewayError(deadline_exceeded)`; after the fix it returns normally and the
admission row is inactive.

## Verification

Commands run against the PG18 test binaries and the shared project virtualenv:

- Regression test before the fix: **failed** with
  `GatewayError: deadline_exceeded` from `_release_admission`.
- Focused PostgreSQL tests after the fix:
  `tests/gateway/integration/test_provider_admission_wait.py`
  `tests/gateway/integration/test_final_capacity_regressions.py` — **18 passed
  in 8.88s**.
- `ruff check` on the changed production/test files — **All checks passed**.
- `python -m compileall -q autobuild_json/gateway/providers/limits.py` — passed.
- `git diff --check` — passed.
- Full `tests/gateway` suite with the same PG18 environment — **1697 passed
  in 315.88s**, with one dependency deprecation warning.

No gateway restart, upstream request, live database write, data symlink
change, push, or merge was performed.
