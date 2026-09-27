# SDD ledger — plan: docs/superpowers/plans/2026-09-27-bounded-admission-acceptance-repair.md

## Setup

- Workspace: `/home/mquangprovip0503/Work/Code/AutoBuildJsonCockplit-Tools/.worktrees/bounded-admission-capacity`
- Branch: `feature/bounded-admission-capacity`
- Base commit for this repair plan: `713cd13` (plan documentation commit: `cab803f`)
- Existing untracked `data` symlink and handoff report are preserved; do not remove or modify the symlink while the user service is running.

## Pre-flight plan scan

| Row | Shared interface/files | Check | Ruling |
|---|---|---|---|
| Task 1 → Task 2 | `ProxyManager.acquire`, lease-store deadline | Task 1 defines the capacity signal; Task 2 hardens the underlying claim deadline | Clean; Task 2 must preserve Task 1's error classification |
| Task 1 → Task 3 | `ProxyManager.snapshot`, Engine publish hook | Task 1 records bounded resource wait data; Task 3 publishes it through the lifecycle owner | Clean; no second sampler or detached writer |
| Task 2 → Task 4 | provider/proxy cleanup and deadline errors | Task 4 measures rows and errors after the final code | Clean; no live run before lock tests and review |
| Task 3 → Task 4 | serving snapshot schema/status | Task 3 owns freshness/unavailable semantics; Task 4 records only observed values | Clean; no idle admin fallback |
| Task 1 | tests exercise actual ProxyManager busy path and Engine release ordering | Agreement: Interfaces and tests match | Clean |
| Task 2 | provider and PgLeaseStore use one remaining deadline | Agreement: no post-expiry insert/claim | Clean |
| Task 3 | publisher start/notify/stop and status states are explicit | Agreement: no detached tasks | Clean |
| Task 4 | real proxies/upstream and cleanup evidence are required | Agreement: synthetic results are not acceptance evidence | Clean |

## Rulings

- Ruling: continue on the existing feature worktree and retain the current provider limit 16 and proxy endpoint exclusivity — the unresolved review findings are repair work on this branch, and changing limits would invalidate the prior evidence and broaden scope; cost if wrong: the branch may need rebasing before integration.
- Ruling: keep the persistent `data` symlink untouched during all verification and treat any ambiguous target as a stop condition — the prior run caused a PostgreSQL panic when it was removed; cost if wrong: a stale service may require operator recovery rather than automated restart.

## Execution

- Task 1 implementer: `/root/repair_proxy_capacity`; base `cab803f`.
- Task 1 implementation complete: commits `cab803f..b53db9f`; focused pytest unavailable in implementer environment, compileall and diff-check passed; task review pending.
- Task 1 fix round 1/5: open Important finding — expired proxy-capacity wait mapped to `proxy_not_ready` and Engine ordering/deadline evidence was not in the task diff; resumed `/root/repair_proxy_capacity`.
- Task 1 fix round 1/5 complete: deadline mapping and Engine/test evidence addressed; scoped re-review approved, pytest unavailable in worker environment.
- Task 1: complete (commits `cab803f..8a1c59e`, review clean with pytest environment limitation recorded).
- Task 1 post-review verification regression: focused real suite shows `test_configured_proxy_failure_does_not_fall_back_direct` receives internal `ProxyCapacityError` message `proxy_capacity` instead of the established public `proxy_not_ready` message for direct non-Engine callers. Resume Task 1 implementer for compatibility fix and regression test before Task 3.
- Task 1 post-review fix: commit `f7181eb`; full focused real suite `45 passed`; scoped re-review pending.
- Task 1 post-review fix complete: class/code remains internal while public message stays `proxy_not_ready`; scoped re-review approved.
- Task 1: complete (commits `cab803f..f7181eb`, review clean; focused real suite `45 passed`).
- Task 3 implementer: `/root/repair_capacity_telemetry`; base `f7181eb`.
- Task 3 implementation complete: commit `6be133c`; Ruff passed, unit engine admission passed, PostgreSQL status integration blocked by missing bundled PG17/test URL; reviewer pending. Existing status expectation likely needs contract update.
- Task 3 amended implementation: commit `d885e26` adds publisher unit tests; review found checked-in status contract/test regression and lifespan startup/shutdown cleanup gap. Fix round 1 dispatched to `/root/repair_capacity_telemetry`.
- Task 3 fix round 1 implementation: commit `49d3660`; status lifecycle tests, lifespan cleanup nesting, coalescer stale-event guard, and bounded stop join added; PG18 integration attempt blocked by initdb dynamic linker; scoped re-review pending.
- Task 3 fix round 1 re-review: status/lifespan/coalescing findings addressed; cancellation-resistant publisher still leaves pending task after bounded stop. Fix round 2 dispatched to `/root/repair_capacity_telemetry`.
- Task 3 fix round 2 implementation: commit `513b9eb`; bounded stop now raises explicit failure when task cannot be joined and retains ownership; publisher tests `3 passed`, Ruff passed; scoped re-review pending.
- Task 3 fix round 2 complete: cancellation-resistant stop finding addressed with explicit bounded `TimeoutError` and retained ownership; scoped re-review approved (`20 passed` focused publisher/engine/settings).
- Task 3: complete (commits `f7181eb..513b9eb`, review clean; PostgreSQL status integration remains environment-blocked by initdb dynamic linker).

## Final whole-branch review

- Reviewer: `/root/whole_branch_review`; package `review-3d58ea6..ef43f12.diff`; verdict code readiness **No**.
- Important findings: truthful resource wait metrics/episodes, telemetry transition notifications + heartbeat, wall-clock DB/cleanup deadlines and expiry mapping, proxy claim acquisition cap, migration head test; final live evidence remains a separate operational blocker.
- One consolidated fix wave dispatched to `/root/whole_branch_fix_wave`; no live restart/burst until scoped re-review approves code fixes.
- Final fix wave revision: `8292d45` (report-only amendment after code `3693a92`; gateway/unit PG18 sweep `768 passed`). Scoped re-review: F1/F3/F5 addressed; F2 transition notifications and F4 unbounded proxy disable/cleanup DB paths remain Important and unresolved. No live restart/burst performed.
- Ruling: do not restart, deploy, merge, or push while F2/F4 remain unresolved — telemetry can be stale during active resource waits and proxy cleanup can still pin provider capacity beyond the acquisition cap; cost if wrong: the gateway may appear healthy while capacity accounting is misleading or a provider slot is held by a blocked proxy cleanup.
- Task 2 implementer: `/root/repair_deadline_locks`; base `8a1c59e`.
- Task 2 implementation complete: commit `b971d79`; focused new tests passed, one timing-sensitive existing test passed alone, and one review regression conflicts with Task 1's deliberate provider wait mode; task review pending.
- Task 2 fix round 1/5: open Important findings — provider-admission and proxy-lease cleanup UPDATEs lacked deadline-aware lock timeout; bounded resource wait sample interface also needs explicit coverage. Resumed `/root/repair_deadline_locks`.
- Task 2 fix round 1/5 implementation: commit `e37c454`; cleanup lock regressions and bounded provider wait samples added; scoped re-review pending. Existing Task1 ProxyCapacityError compatibility regression remains separately tracked.
- Task 2 fix round 1/5 re-review: provider cleanup and bounded samples addressed; proxy release remains cancellation-unsafe. Fix round 2 dispatched to `/root/repair_deadline_locks`.
- Task 2 fix round 2 implementation: commit `d477ffc`; proxy release owns/shields bounded cleanup and adds cancellation regression; scoped re-review pending.
- Task 2 fix round 2 complete: commits `b971d79..d477ffc`, review clean; provider/proxy cleanup and wait samples are bounded, focused cleanup suite `15 passed`.

## Final consolidated fix wave

- Commit `c8a83eb` closes whole-branch review findings 1–5: truthful admitted resource-wait episodes and bounded uncapped samples, lifecycle transition notifications plus 10-second heartbeat, wall-clock transaction/cleanup deadlines with redacted expiry mapping, acquisition-capped proxy store operations preserving request lease deadlines, and migration head/table contract `0016_capacity_snapshots`.
- Fresh focused PostgreSQL-capable/unit/status command: `AUTOBUILD_TEST_POSTGRES_BIN=.deps/gateway-pg18/root/usr/lib/postgresql/18/bin LD_LIBRARY_PATH=.deps/gateway-pg18/root/usr/lib/x86_64-linux-gnu:.deps/gateway-pg18/root/usr/lib/postgresql/18/lib /home/mquangprovip0503/Work/Code/AutoBuildJsonCockplit-Tools/.venv/bin/python -m pytest tests/gateway/unit/test_admission.py tests/gateway/unit/test_capacity_publisher.py tests/gateway/unit/test_engine_resource_order.py tests/gateway/unit/test_proxy_manager.py tests/gateway/integration/test_final_capacity_regressions.py tests/gateway/integration/test_provider_admission_wait.py tests/gateway/integration/test_proxy_leases.py tests/gateway/integration/test_catalog_schema.py::test_catalog_upgrade_from_revision_nine_preserves_existing_rows tests/gateway/integration/test_codex_service_status.py -q --tb=short` → `65 passed in 16.75s`.
- Ruff, compileall, and `git diff --check` pass. Final live restart/burst remains intentionally separate; no service restart or persistent `data` symlink mutation performed.

## Residual repair wave

- Root cause: resource wait begin/end transitions did not notify the serving publisher; direct routes were counted as zero-duration proxy waits; non-deadline resource failures were labeled success. Proxy invalidation/cleanup store calls also lacked acquisition-budget propagation, and timed-out cleanup could discard unreleased tokens.
- Changes: Engine now publishes on resource-wait begin/end and distinguishes direct selection; admission snapshots include failed resource waits; proxy health/cleanup calls use bounded store operations; cleanup removes tokens only after successful release so timed-out tokens remain available to a bounded finally retry; the provider concurrency regression now asserts intentional wait-and-release semantics.
- RED evidence: `test_route_resources_publishes_resource_wait_transitions`, `test_cleanup_owned_retains_token_when_release_budget_expires`, and the direct-selection telemetry regression failed before the corresponding production changes.
- GREEN evidence: PG18 focused command covering admission, publisher, Engine ordering, proxy manager, proxy leases, provider wait, service status, final capacity regressions, and the updated concurrency contract → `70 passed in 13.03s`; Ruff, compileall, and diff-check pass. Full PG18 gateway run after the wait-contract update → `1687 passed, 1 warning` (before the final token-retention amendment); the focused run includes the amendment.
- Scope remains isolated to telemetry/capacity and tests. No restart, deploy, push, or persistent `data` symlink mutation has occurred.
- Follow-up scoped review found three residuals: health-disable did not receive the acquisition deadline, permanent Kiot errors could be masked as `proxy_capacity` by cleanup expiry, and unexpected provider/proxy exceptions were recorded as successful waits. Added RED tests for all three plus transient lease-release retry; production now passes wall-clock health deadlines, preserves primary Kiot failures, retries bounded cleanup, and records generic failures as `failed`.
- Final focused PG18 command after this follow-up → `76 passed in 13.16s`; Ruff, compileall, and diff-check remain clean. Scoped reviewer found no Critical issues and no further Important issue in the token-retention path; persistent-store-failure residue remains governed by bounded PostgreSQL cleanup/fencing and is explicitly not claimed as zero-leak evidence.
