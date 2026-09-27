# Task 2 report: total provider/proxy request-deadline enforcement

## Status

Implemented and committed as `fix: bound provider and proxy lock waits by request deadline`.

## Changes

- Added a remaining-budget lock-timeout helper to `ProviderLimits` and refreshed the transaction-local timeout before provider locking, credential locking, and admission insertion.
- Added deadline-aware lock timeout configuration to `PgLeaseStore.claim` before its conflict-sensitive insert, lease-row `FOR UPDATE`, and ownership update.
- Added post-lock and post-update deadline checks. Expired operations roll back and map PostgreSQL lock timeout (`55P03`) to the existing redacted `deadline_exceeded` gateway error.
- Added PostgreSQL regressions covering cumulative provider/credential lock waits and a proxy lease-row lock held beyond the claim deadline. Both assert no late admission/ownership.

## Verification

PostgreSQL fixture command/environment:

```text
AUTOBUILD_TEST_POSTGRES_BIN=$PWD/../../.deps/gateway-pg18/root/usr/lib/postgresql/18/bin
LD_LIBRARY_PATH=$PWD/../../.deps/gateway-pg18/root/usr/lib/x86_64-linux-gnu:$PWD/../../.deps/gateway-pg18/root/usr/lib/postgresql/18/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
../../.venv/bin/python -m pytest ...
```

The new RED run failed as expected: both tests were cancelled by the outer `wait_for` while production code remained blocked in the second provider lock / proxy lease lock.

The focused new tests passed: **2 passed**.

The provider and proxy integration files were run together: **11 passed, 1 failed**. The failure was the existing review test `test_deadline_is_rechecked_after_a_provider_row_lock`, which was timing-sensitive in the combined run (`pending.done()` was false after 150 ms); rerunning that test alone passed (**1 passed**).

The full provider/proxy/review command reached **20 passed, 1 failed**. The review failure was `test_provider_concurrency_is_shared_across_client_keys`, receiving `deadline_exceeded` instead of `upstream_unavailable`; this follows the prior task-1 engine behavior passing `wait=True` for provider admission and is outside this task's provider/proxy policy scope.

`git diff --check` passed.

## Concerns

- The review-regression expectation for immediate engine-level provider rejection conflicts with the task-1 engine path that intentionally calls `ProviderLimits.acquire(..., wait=True)`. No engine/provider policy change was made here.
- The existing 50 ms provider-lock regression can be flaky under combined PostgreSQL fixture startup/load; it passed when rerun alone.

## Review fix round 1

- Bounded provider-admission and proxy-lease cleanup updates with a documented 250 ms independent cleanup grace. Lock timeouts map to redacted `deadline_exceeded`; provider cleanup runs in an owned shielded task so cancellation cannot abandon it mid-query.
- Added cleanup-row lock regressions: **2 passed**.
- Added bounded provider wait samples (`wait_p50_ms`, `wait_p95_ms`, `last_wait_ms`) to `ProviderLimits.snapshot()` using a capped deque.
- Targeted ruff and diff checks: passed.
- Task-1's unrelated `ProxyCapacityError` public-message regression was intentionally not changed.

## Review fix round 2

- `PgLeaseStore.release` now owns its bounded cleanup task and shields/joins it through cancellation, propagating cancellation only after the 250 ms lock-timeout grace completes.
- Added cancellation-during-held-lease-row regression; cleanup timeout and cancellation regressions passed (**2 passed**).
- Targeted provider cleanup regression plus lease cancellation regression: **2 passed**; ruff passed.
