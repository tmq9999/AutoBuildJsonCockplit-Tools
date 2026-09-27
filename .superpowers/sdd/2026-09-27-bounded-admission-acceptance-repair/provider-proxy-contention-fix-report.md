# Provider and proxy contention repair

## Root cause

Capacity waiters woke together every 50 ms and each opened a PostgreSQL
transaction, locked the same provider row, and counted admissions. Under 100
waiters this exhausted the shared pool and delayed release cleanup. Proxy
cleanup had the same boundary: a single bounded release attempt could leave a
lease owner set until hard expiry when the pool or lease row was busy.

## Design and fix

`ProviderLimits` now uses a bounded process-local FIFO `asyncio.Lock` per
provider. The lock covers only one authoritative database admission attempt;
it is released before the wait condition or upstream work. A bounded overflow
lock prevents unbounded key memory while preserving cross-provider parallelism.

`ProxyManager` now uses the same bounded per-resource lock for each lease-store
database operation, including claim, health, renew, and release. It is never
held during Kiot/network calls. Proxy cleanup retries remain bounded and count
unresolved tokens as `cleanup_failures`; generation and hard-deadline fencing
remain authoritative.

## Regression evidence

- Before the provider gate, a real PostgreSQL test with a three-connection pool
  and 100 same-provider waiters failed with `deadline_exceeded`.
- After the provider gate, the same test admitted and cleaned all 100 waiters;
  no active provider rows or cleanup failures remained.
- A real PostgreSQL proxy cleanup test with a three-connection pool releases a
  lease after transient contention; no owner row remains and proxy cleanup
  failures stay zero.
- Focused provider, proxy, final-capacity, and proxy-manager tests passed after
the fix: **53 passed in 23.37s**. The full `tests/gateway` suite then passed
**1699 tests in 551.34s**, with one dependency deprecation warning. Ruff,
compileall, and `git diff --check` passed.

The final review hardening reserves lock-map entries synchronously with a
`users` count, uses stable sharded overflow locks, closes unstarted store
coroutines on deadline/cancellation, and catches the outer cleanup timeout so
it cannot replace a primary error. The post-hardening focused provider/proxy
regressions passed **55 tests**; unit/final-capacity follow-up passed **39**.

Overflow migration is also fenced: once a key falls into a shard, a bounded
overflow-key map retains that assignment so a later same-key lookup cannot
create a dedicated lock while the shard lock is held. Deterministic capacity=1
tests cover this migration for provider and proxy keys.

No live inference, gateway restart, live database write, data symlink change,
push, or merge was performed.
