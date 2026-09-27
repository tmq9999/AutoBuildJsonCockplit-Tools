# Bounded Admission Settlement and Proxy Failure Repair Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate avoidable interrupted usage holds and transient proxy failures observed during the real 100-user workflow while preserving exact accounting, bounded cleanup, and the current provider/proxy limits.

**Architecture:** Keep the process-local admission capacity at 100, provider concurrency at 16, and one-owner proxy leases. Add explicit dispatch-certainty classification and safe route retry only when connection establishment proves the request was not transmitted; make cancellation/stream cleanup and recovery observable and exactly-once without fabricating usage. A request with unknown upstream usage remains `usage_pending` until authoritative usage exists.

**Tech Stack:** Python 3.11+, asyncio, FastAPI/Starlette, SQLAlchemy async PostgreSQL, httpx/httpcore, pytest/pytest-asyncio, existing real-proxy acceptance harness.

**Spec:** `docs/superpowers/specs/2026-09-27-bounded-admission-proxy-capacity-design.md` and the evidence in `docs/research/2026-09-27-bounded-admission-acceptance-final.md`.

## Global Constraints

- Keep process-local inference admission capacity at `100`.
- Keep provider/credential concurrency limit at `16` and proxy endpoint ownership at one request per endpoint.
- Never fall back to direct egress when a route requires a proxy.
- Never fabricate input, cached, output, or reasoning usage; unknown upstream usage remains pending.
- Retry only a proxy connection-establishment failure that proves the request was not transmitted; absence of response headers alone is insufficient.
- Do not expose emails, API keys, OAuth tokens, proxy URLs, raw upstream errors, or raw response artifacts.
- Every cleanup/recovery change must preserve fencing, bounded deadlines, idempotent ledger settlement, and quota holds for unknown usage.
- Acceptance uses real upstream responses and the configured 20 HTTP proxies; mock results are regression evidence only.

## Review Focus

- Client disconnect immediately after upstream headers: stream cancellation must not double-settle, leak a provider/proxy permit, or silently release unknown quota.
- Proven proxy connection-establishment failure: this may be retried on another configured endpoint after releasing the first provider/proxy resources. A reset after transmission remains pending, even without response headers.
- Proxy failure after response bytes: it must remain a single `proxy_error`/pending outcome with no retry.
- Recovery with an interrupted request whose attempt later receives authoritative usage: settlement must happen once; unknown usage must remain pending and visible.
- Request deadline during provider/proxy wait: the error must identify the wait stage, preserve primary errors, and leave no live rows.

### Task 1: Reproduce and classify workflow failures

**Files:**
- Modify: `autobuild_json/gateway/transport/http.py`
- Modify: `autobuild_json/gateway/transport/websocket.py`
- Modify: `autobuild_json/gateway/errors.py`
- Modify: `autobuild_json/gateway/engine.py`
- Test: `tests/gateway/unit/test_gateway_transport.py`
- Test: `tests/gateway/unit/test_engine_resource_order.py`
- Test: `tests/gateway/integration/test_gateway.py`

**Interfaces:**
- Add an internal transport failure classification (not a new secret-bearing public payload) that records `before_response` versus `after_response` and whether a proxy was used.
- `GatewayError.safe_retry` is true only when a proxy connection-establishment failure proves the request was not transmitted; read/write/protocol failures remain false even without response headers.
- `Engine.prepare()`/route dispatch retries this class only through an untried route and after the current resource context has exited.

- [ ] **Step 1: Write failing tests** for a proxy `ConnectError` before headers being retryable, a reset after one streamed chunk not being retryable, and provider/proxy resources being released before the retry.
- [ ] **Step 2: Run the focused tests and verify RED** with `pytest tests/gateway/unit/test_gateway_transport.py tests/gateway/unit/test_engine_resource_order.py tests/gateway/integration/test_gateway.py -q`; expected failure because network errors are currently all mapped to the same `proxy_error` path.
- [ ] **Step 3: Implement phase-aware transport errors** without exposing exception text. Set the phase at response acquisition/first-byte boundaries; preserve `deadline_exceeded` and upstream HTTP rejection behavior.
- [ ] **Step 4: Wire one safe retry** through the existing dispatch route loop. Do not retry after headers/bytes, do not retry ambiguous WebSocket streams, and do not hold a provider admission while selecting the next proxy.
- [ ] **Step 5: Run focused tests and Ruff**; expected all new tests pass and existing gateway transport/resource tests remain green.
- [ ] **Step 6: Commit** `fix: classify and safely retry pre-response proxy failures`.

### Task 2: Make interrupted usage settlement explicit and recoverable

**Files:**
- Modify: `autobuild_json/gateway/engine.py`
- Modify: `autobuild_json/gateway/maintenance.py`
- Modify: `autobuild_json/gateway/ledger.py` (or the existing ledger service owning pending settlement)
- Test: `tests/gateway/integration/test_gateway.py`
- Test: `tests/gateway/integration/test_recovery.py`
- Test: `tests/gateway/unit/test_engine_admission.py`

**Interfaces:**
- Preserve `PreparedCall.close()` and cleanup as idempotent; it must release resources once and mark `usage_pending/interrupted` only after dispatch without authoritative usage.
- Recovery may finalize a pending request only when its attempt contains authoritative usage; it must never infer usage from request bounds or elapsed time.
- Add a redacted pending-age/recovery outcome to maintenance/status snapshots so operators can distinguish recoverable pending usage from unknown usage.

- [ ] **Step 1: Write failing integration tests** for disconnect after dispatch with no usage (hold remains pending), late authoritative attempt usage (settles exactly once), double `close()`/generator finalization (no duplicate ledger entry), and pre-dispatch cancellation (released as `not_dispatched`).
- [ ] **Step 2: Run the focused PostgreSQL tests and verify RED** against the bundled PostgreSQL environment.
- [ ] **Step 3: Implement the smallest cleanup/recovery change**: join the same owned cleanup task, persist a recovery marker/age, and make maintenance settle only saved authoritative usage. Keep unknown usage pending.
- [ ] **Step 4: Run recovery, gateway stream, quota, and engine-admission tests**; assert provider-admission and proxy-owner counts are zero after each case.
- [ ] **Step 5: Commit** `fix: reconcile interrupted usage exactly once`.

### Task 3: Bound deadline waits and expose useful failure-stage telemetry

**Files:**
- Modify: `autobuild_json/gateway/engine.py`
- Modify: `autobuild_json/gateway/providers/limits.py`
- Modify: `autobuild_json/gateway/proxy/manager.py`
- Modify: `autobuild_json/gateway/admission.py`
- Test: `tests/gateway/unit/test_engine_resource_order.py`
- Test: `tests/gateway/unit/test_admission.py`
- Test: `tests/gateway/integration/test_provider_admission_wait.py`
- Test: `tests/gateway/integration/test_final_capacity_regressions.py`

**Interfaces:**
- Capacity snapshots report provider wait, proxy wait, queue wait, deadline expiry, and cancellation separately; samples remain bounded.
- `route_resources()` maps a request deadline to the actual waiting stage (`provider`, `proxy`, or `upstream`) while preserving the primary error and releasing resources before returning.
- No provider/proxy limit is raised and no direct fallback is introduced.

- [ ] **Step 1: Write failing tests** for cumulative provider+proxy waits consuming one request deadline, proxy wait expiry releasing the provider slot first, and failure-stage telemetry distinguishing capacity expiry from upstream rate limiting.
- [ ] **Step 2: Run the focused tests and verify RED**.
- [ ] **Step 3: Implement shared remaining-budget propagation and stage accounting**; keep polling/notifications bounded and avoid holding SQL sessions during waits.
- [ ] **Step 4: Run the focused PostgreSQL/resource suite and verify no active admission or lease rows remain after expiry/cancellation.
- [ ] **Step 5: Commit** `fix: report bounded resource deadline outcomes`.

### Task 4: Final real acceptance and rollout decision

**Files:**
- Modify if required: operational `data/bounded_acceptance_verify.py` (ignored; no secrets/artifacts committed)
- Create: `docs/research/2026-09-27-bounded-admission-settlement-live.md`

**Interfaces:**
- Run against the pushed branch revision with capacity 100, provider limit 16, and the same 20 HTTP proxies.
- Record only redacted aggregates: status/error stage, pre-response retries, usage totals, ledger mismatch count, pending-age states, queue/provider/proxy waits, health latency, active-row cleanup, and key revocation.

- [ ] **Step 1: Run full offline verification**: `pytest tests/gateway -q`, Ruff, compileall, and diff-check.
- [ ] **Step 2: Restart the service at the reviewed revision**, preserving the `data` symlink; verify `/health` and admin status.
- [ ] **Step 3: Run real single non-stream/stream requests** and verify usage, ledger, and cleanup.
- [ ] **Step 4: Run the real 100-key burst and the 3-step workflow** without client retries except the harness's explicitly documented pre-response proxy retry.
- [ ] **Step 5: Require every request with an authoritative terminal usage to be completed and settled exactly once; report any remaining unknown `usage_pending` requests separately rather than charging or silently releasing them.
- [ ] **Step 6: Commit the redacted report** as `docs: record settlement repair acceptance`.
- [ ] **Step 7: Push the repair branch and open/update a PR only after review and evidence; do not merge or raise provider/proxy limits automatically.

## Rollback

If live verification regresses, revoke temporary keys, verify proxy/provider cleanup, and restart the previously pushed `e316195` behavior revision. Do not delete database rows, disable proxy enforcement, or increase concurrency as an emergency workaround.

## Self-review checklist

- Every observed outcome from the final live run has a task: 3 interrupted pending settlements (Task 2), 2 proxy errors (Task 1), and 1 deadline expiry (Task 3).
- The plan preserves the safety rule that unknown upstream usage cannot be fabricated or released automatically.
- Retry scope is limited to proven proxy connection-establishment failures before request transmission; unknown dispatch and stream outcomes remain non-retryable.
- The final task uses real upstream/proxy evidence and separates gateway capacity from upstream limitations.
