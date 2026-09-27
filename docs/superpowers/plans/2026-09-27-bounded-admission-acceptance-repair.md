# Bounded Admission Acceptance Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining acceptance blockers in the bounded-admission gateway so the service can be deployed and measured honestly for 100 accepted users over the configured proxy pool.

**Architecture:** Preserve the existing process-local 100-request admission gate, provider limit of 16, and one-owner proxy leases. Repair the resource-wait contracts and deadline propagation, replace detached per-request telemetry writes with one lifecycle-owned coalescing publisher, and make the admin view report only serving-process data or an explicit unavailable/stale state. Finish with a real restart and a 100-request live run; no synthetic result is accepted as deployment evidence.

**Tech Stack:** Python 3.11+, asyncio, FastAPI/Starlette lifespan, SQLAlchemy async PostgreSQL, PostgreSQL lock timeouts, pytest/pytest-asyncio, Ruff, existing HTTP proxy transport and operational verification script.

**Spec:** `docs/superpowers/specs/2026-09-27-bounded-admission-proxy-capacity-design.md`

## Global Constraints

- Default process-local in-flight capacity remains `100`; request 101 receives the documented safe `gateway_busy` response with `Retry-After`.
- The request deadline is authoritative across every provider/proxy lock, wait, insert, lease, stream, and cleanup operation; no operation may insert or claim after expiry.
- Provider/credential limits remain authoritative and the first rollout keeps the live provider limit at `16`.
- Static HTTP proxy endpoints and Kiot key/endpoint pairs remain exclusive; direct fallback is never implicit.
- Queue expiry/cancellation releases an unspent ledger hold as `not_dispatched`; real input/cached/output/reasoning usage is settled exactly once from the real response.
- Admin capacity telemetry must describe the serving process; an idle admin-process Engine is never a substitute for missing serving telemetry.
- Do not expose credentials, OAuth tokens, API keys, emails, proxy URLs/secrets, raw upstream errors, or raw benchmark artifacts in source, logs, reports, or responses.
- Do not merge, push, or restart the user service until the final whole-branch review has no unresolved Important/Critical finding and the `data` symlink has been verified intact.
- Final acceptance must use the configured 20 HTTP proxies, real upstream responses, 100 distinct temporary keys, and cleanup in `finally`; mock/lab output cannot be reported as the capacity result.

## Review Focus

- A real `ProxyManager` with all leases busy must produce the internal retryable capacity signal for `wait=False`; permanent disabled/invalid/Kiot failures must not be retried (Task 1).
- Sequential provider and proxy row-lock waits must consume one shared deadline budget, abort before insert/claim after expiry, and leave no admission/lease rows (Task 2).
- A serving process must publish bounded/coalesced telemetry and stop its publisher on lifespan shutdown; missing schema/row must be `unavailable`, not an idle local snapshot (Task 3).
- Provider/proxy wait duration percentiles must be measured from actual waits and bounded in memory, not inferred from request latency or fabricated (Task 3).
- The final 100-user run must separate accepted, queued, completed, expired, genuine upstream failures, queue/resource p50/p95, health latency, active maxima, usage reconciliation, and cleanup; no “100 users succeeded” claim is allowed without those fields (Task 4).

---

### Task 1: Repair proxy-capacity signaling and real Engine composition

**Files:**
- Modify: `autobuild_json/gateway/proxy/manager.py`
- Modify: `autobuild_json/gateway/engine.py`
- Test: `tests/gateway/unit/test_proxy_manager.py`
- Test: `tests/gateway/unit/test_engine_resource_order.py`
- Create if needed: `tests/gateway/integration/test_proxy_engine_capacity.py`

**Interfaces:**
- `ProxyManager.acquire(selection, owner, deadline, *, wait=False)` raises the internal `ProxyCapacityError` only when an eligible local lease is busy; it returns/raises existing permanent `GatewayError` codes for disabled, invalid, unavailable, or expired proxy state.
- `ProxyManager.wait_for_capacity(...)` waits only for the internal capacity signal and never holds a provider admission or DB session while waiting.
- `Engine.route_resources(...)` passes `wait=False` for the proxy attempt, exits the provider context before waiting, and retries only `ProxyCapacityError` while the same request deadline remains valid.
- `ProxyManager.snapshot()` exposes bounded capacity-signal and wait-duration counters without endpoint identity.

- [ ] **Step 1: Write failing tests** using the real `ProxyManager` composition (a deterministic lease-store double is allowed, but not a fake proxy that raises the signal itself): all static endpoints busy gives `ProxyCapacityError` for `wait=False`; a disabled endpoint gives `proxy_not_ready` without retry; Kiot invalid/unavailable remains immediate; Engine releases provider before `wait_for_capacity` and then retries.
- [ ] **Step 2: Run the focused tests and verify RED** with `pytest tests/gateway/unit/test_proxy_manager.py tests/gateway/unit/test_engine_resource_order.py tests/gateway/integration/test_proxy_engine_capacity.py -q`; expected failure on the actual `wait=False` busy path.
- [ ] **Step 3: Implement the explicit signal contract** in `ProxyManager.acquire` and Engine resource ordering. Keep endpoint exclusivity, the existing 10-second per-acquisition cap (shortened by the request deadline), Kiot rotation, cancellation cleanup, and no direct fallback. Record actual proxy wait duration in a bounded sample ring.
- [ ] **Step 4: Run the focused tests and relevant quota/cleanup tests** with `pytest tests/gateway/unit/test_proxy_manager.py tests/gateway/unit/test_engine_resource_order.py tests/gateway/integration/test_proxy_engine_capacity.py tests/gateway/integration/test_codex_quota.py -q`; expected PASS.
- [ ] **Step 5: Commit** `fix: align proxy capacity signal with engine retry path`.

### Task 2: Enforce one total deadline across provider and proxy DB operations

**Files:**
- Modify: `autobuild_json/gateway/providers/limits.py`
- Modify: `autobuild_json/gateway/proxy/pg_leases.py`
- Test: `tests/gateway/integration/test_provider_admission_wait.py`
- Test: `tests/gateway/integration/test_proxy_leases.py`
- Test: `tests/gateway/integration/test_review_regressions.py`

**Interfaces:**
- Add one internal remaining-deadline helper used before every blocking SQL operation; each `FOR UPDATE`, conflict-sensitive insert, and update receives a transaction-local `lock_timeout` based on the current remaining budget, not the budget captured at transaction start.
- `ProviderLimits.acquire(..., wait=True)` never inserts `provider_admissions` after the deadline and reports `deadline_exceeded` rather than a generic upstream failure when a lock timeout consumes the budget.
- `PgLeaseStore.claim(resource, owner, deadline)` applies the same rule to lease-row creation/locking and never returns a token after expiry; lock timeout errors map to the existing redacted deadline error.
- Provider/proxy wait snapshots include bounded actual wait samples and remain safe if cleanup itself is cancelled.

- [ ] **Step 1: Write failing PostgreSQL tests** that hold the provider row lock while the credential lock becomes available only after the original timeout, hold the proxy lease row lock past the deadline, and verify no admission/lease row is inserted or owned. Preserve the existing immediate `wait=False` behavior.
- [ ] **Step 2: Run the focused PostgreSQL tests and verify RED** with the repository's bundled PostgreSQL command/environment; expected failure because the second lock and proxy claim use stale/no timeout budgets.
- [ ] **Step 3: Implement remaining-budget lock configuration and post-lock/post-insert deadline checks**. Use short committed transactions, preserve error codes, and do not add a retry that can outlive the request.
- [ ] **Step 4: Run focused provider/lease tests plus the existing review regressions** and record exact counts, including the PostgreSQL setup command used.
- [ ] **Step 5: Commit** `fix: bound provider and proxy lock waits by request deadline`.

### Task 3: Make serving telemetry lifecycle-owned, bounded, and truthful

**Files:**
- Create: `autobuild_json/gateway/capacity.py` (only if a focused publisher class keeps lifecycle logic out of Engine)
- Modify: `autobuild_json/gateway/engine.py`
- Modify: `autobuild_json/gateway/http/app.py`
- Modify: `autobuild_json/gateway/admin/codex_accounts.py`
- Modify: `tests/gateway/integration/test_codex_service_status.py`
- Create/modify: `tests/gateway/unit/test_capacity_publisher.py`

**Interfaces:**
- `Engine.start_capacity_publisher()` creates at most one task owned by the serving app; `Engine.request_capacity_publish()` coalesces updates; `Engine.stop_capacity_publisher()` signals, flushes best-effort within a bounded timeout, and joins the task.
- Publishing is rate-limited/coalesced (one writer at a time) and serializes bounded admission/provider/proxy snapshots with actual wait p50/p95; request paths never create detached publisher tasks.
- `ServiceStatus.read()` returns the serving snapshot when fresh, `{"stale": true, "updated_at": ...}` when older than the documented threshold, and `{"available": false, "reason": "serving_snapshot_unavailable"}` when the table/row/schema is unavailable. It never falls back to `services.engine.capacity_snapshot()`.
- `/health` remains independent of admission and status telemetry.

- [ ] **Step 1: Write failing tests** for publisher coalescing (many notifications produce one bounded write), shutdown joining/no pending task, bounded wait-sample memory, fresh/stale/unavailable admin payloads, and health success while an inference permit is held.
- [ ] **Step 2: Run the focused telemetry/status tests and verify RED** with `pytest tests/gateway/unit/test_capacity_publisher.py tests/gateway/integration/test_codex_service_status.py -q`.
- [ ] **Step 3: Implement the lifecycle-owned publisher and remove detached `_publish_capacity()` task creation**. Start/stop it from the FastAPI lifespan used by the serving process; keep admin status read-only and redacted. Preserve the migration/schema already present and do not add a second telemetry table.
- [ ] **Step 4: Run focused status, boundary, settings, and engine-cleanup tests** with `pytest tests/gateway/unit/test_capacity_publisher.py tests/gateway/integration/test_codex_service_status.py tests/gateway/unit/test_engine_admission.py tests/gateway/unit/test_service_setup.py -q` plus Ruff for changed files.
- [ ] **Step 5: Commit** `fix: publish bounded serving capacity telemetry`.

### Task 4: Final verification, safe rollout, and live acceptance evidence

**Files:**
- Modify if required: `data/proxy_load_verify.py` (operational benchmark only; no secrets/artifacts committed)
- Create: `docs/research/2026-09-27-bounded-admission-acceptance-repair-live.md`
- Modify: `.superpowers/sdd/2026-09-27-bounded-admission-capacity/progress.md` in the feature worktree only

**Interfaces:**
- The verification uses the accepted feature-branch revision, `AUTOBUILD_GATEWAY_INFERENCE_CAPACITY=100`, the existing provider limit `16`, the configured 20 HTTP proxies, 100 distinct temporary API keys, real `/v1/responses`, and cleanup/revocation in `finally`.
- The report contains only redacted aggregate evidence: exact code revision, command/environment labels, focused/full test results, real stream/non-stream status and usage presence, accepted/queued/completed/expired counts, genuine error codes by stage, queue/provider/proxy wait p50/p95, end-to-end p50/p95, health max latency, provider-active max, proxy-lease max, DB connection/lock observations, ledger settlement reconciliation, probe results, and cleanup counts.
- No report claims zero leaks, 100 successful completions, or 503-free operation unless the corresponding SQL/HTTP evidence was collected after the final revision and reconciled.

- [ ] **Step 1: Record a clean baseline and run offline verification** from the feature worktree: `.venv/bin/python -m pytest -q`, `ruff check autobuild_json tests`, and `.venv/bin/python -m compileall -q autobuild_json`; separate assertion failures from PostgreSQL fixture/setup failures.
- [ ] **Step 2: Run the whole-branch review** against the feature branch after Tasks 1–3; resolve every Important/Critical finding with one reviewed fix wave before any service restart.
- [ ] **Step 3: Verify runtime paths before restart**: confirm branch/revision, resolve the persistent `data` symlink target, confirm migrations and proxy configuration, then restart the gateway with capacity 100. Abort if the symlink or database path is ambiguous; never delete or recreate it as part of the test.
- [ ] **Step 4: Execute real single non-stream and stream requests** through the public listener and verify non-empty output, usage fields, `response.completed` for stream, persisted ledger settlement, fresh serving telemetry, and zero active admission/provider/proxy rows for those requests.
- [ ] **Step 5: Run the 100-user burst** with no client retry, then the secondary workflow mode. Use the configured proxies `192.168.1.17:7001`–`192.168.1.17:7020`; record actual response/error bodies only in a protected local artifact and copy aggregates—not secrets or raw responses—into the report.
- [ ] **Step 6: Reconcile and decide rollout**: verify temporary-key revocation, lease/admission cleanup, ledger states and usage totals, status/health responsiveness, and report any genuine upstream 429/5xx or proxy failure. If the deadline prevents completion, classify the result as a measured latency/capacity limit and do not increase provider/proxy concurrency without a new approved plan.
- [ ] **Step 7: Commit only the redacted report/operational changes** with `docs: record final bounded-admission acceptance evidence`; deploy/push only after the user approves the reviewed result.

## Rollback

If live verification exposes a regression, stop the burst, revoke temporary keys, release/verify leases, and restart the prior accepted revision with the existing provider/proxy limits. Do not disable proxy enforcement or raise concurrency as an emergency workaround. A later capacity increase requires a separate measured plan.

## Self-review checklist

- The plan addresses every unresolved load-bearing item in the existing SDD ledger: real proxy/Engine signal mismatch, cumulative provider/proxy deadlines, idle-admin telemetry fallback, detached publisher tasks, bounded samples, and missing final-wave live evidence.
- It does not change OAuth, model catalogs, account rotation, quota semantics, token prices, or direct-proxy policy.
- Every task has an independently testable result and names exact interfaces and files.
- The acceptance task explicitly distinguishes real response evidence from unit/mock results and has a rollback path that preserves the persistent data symlink.
