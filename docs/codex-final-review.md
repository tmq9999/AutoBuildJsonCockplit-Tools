# Independent Codex gateway final review

Reviewed baseline: `2476a73..e280946` against the approved Codex API service spec
and implementation plan. Existing unrelated working-tree edits were not changed.
No live upstream requests, accounts, migrations, or heavy suites were run. Both
findings below were reproduced with isolated in-memory calls to the production
validation functions.

Verdict at the reviewed baseline: changes requested (two P2 correctness findings).

## P2 — Reject a credit snapshot after its latest refresh failed

Location: `autobuild_json/gateway/accounts/quota_storage.py:116–124`.

`consumable()` checks the saved credit timestamp, version, and positive count,
but does not check `account["credits_error"]`. A partial refresh deliberately
preserves the previous snapshot and its timestamp while setting that error
(`quota_store.py:142–151`). The admin projection therefore correctly labels the
snapshot stale, but both reset preflight and dispatch still accept it while its
old timestamp is less than 120 seconds old.

Reproduction: save a positive credit snapshot, make the next explicit credit
refresh fail within 120 seconds, then POST a new confirmation with that saved
snapshot's version. The UI disables reset, but the API can dispatch consume using
the same stale evidence. An isolated call with a recent positive snapshot and
`credits_error="codex_credits_unavailable"` returned normally from `consumable()`.

Required correction: reject a recorded credit-refresh error with the stale-credit
error until a later successful credit fetch clears it. Cover both preflight and
the dispatch recheck.

## P2 — Do not let snapshot age clear known account exhaustion

Location: `autobuild_json/gateway/routing/account_pool.py:34–38`.

The freshness early return runs before inspection of `limit_reached` or a 100%
used window. With the default `reserve_enabled=False`, an account excluded for
known exhaustion becomes eligible automatically when its snapshot is older than
`snapshot_max_age_seconds`, or when a later failed refresh marks it erroneous.
Neither event supplies evidence that upstream quota replenished. This violates
the plan's requirement to require a new observation before clearing exhaustion.

Reproduction: an active pool member has a snapshot with
`primary.used_percent=100` and `limit_reached=True`. At snapshot time the
diagnostic is `pool_quota_exhausted`; 121 seconds later, with no new observation,
the same default-policy candidate returns `None` (eligible) and can be selected
and dispatched again. The existing test at
`tests/gateway/unit/test_account_pool.py:123–128` currently expects this incorrect
transition.

Required correction: retain known exhaustion until successful newer evidence
clears it, independently of whether unknown quota is permitted when reserve mode
is off. Keep genuinely missing/non-exhausted stale evidence's reserve behavior
separate, and update the regression test.

## Resolution and scoped re-review

Both findings were fixed after the baseline review. The reset guard rejects a
non-null `credits_error` at preflight, prepare and dispatch. Exhaustion is checked
before freshness, preserving genuinely missing/non-exhausted stale observations'
existing reserve policy. Real PostgreSQL regressions cover failed credit refresh,
the prepare/dispatch races, and successful refresh recovery; pool tests exercise
all modes and fresh recovery. Controller focused verification: **185 passed**.

The independent reviewer re-read the fix diff and tests and reran the pool suite
(121 passed): both findings addressed, no new actionable finding. This verdict
does not include unrelated uncommitted changes in the checkout.

Known conservative limitation: token-generation/configuration changes after a
successful reset receipt invalidate its original stamp. Later refresh may leave
the operation at `succeeded_refresh_failed`; explicit admin evidence resolution
is required. The original fencing contract is retained.

## Verification of current checkout

Before the two review fixes: Python3.14 full suite **1352 passed**, two dependency
deprecations (Google GenAI and Starlette/AnyIO). Node UI **43 passed**; OAuth,
gateway and Codex browser smoke passed with synthetic provider I/O. The first
OAuth smoke attempt hit a Playwright `Route is already handled` error; its isolated
rerun passed, with no product code change. Codex desktop/mobile screenshots were
inspected. Python3.10 is unavailable in the current checkout; its older 1312-test
checkpoint is historical, not verification of these fixes.

Post-fix full Python3.14 verification: **1445 passed**, two dependency warnings,
396.95 seconds. Ruff, compileall and diff-check passed; sdist/wheel built with
`build --no-isolation` using the preexisting cached setuptools backend. No new
Python3.10 or isolated-build result is claimed.
The Codex browser smoke also passed again after the fixes, including all six pool
and grant review scenarios, reset recovery, masked accounts and mobile layout.

Verification commands used `.venv/bin/python -m pytest -q --tb=short`,
`npm run test:ui`, the three `test:*browser` package scripts, `.venv/bin/ruff check .`,
and `.venv/bin/python -m compileall -q autobuild_json`. PostgreSQL tests received
`AUTOBUILD_TEST_POSTGRES_BIN` and `LD_LIBRARY_PATH` for the existing extracted
PostgreSQL18 installation. Every database was generated by the test harness.
