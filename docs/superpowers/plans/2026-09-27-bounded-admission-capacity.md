# Bounded Admission Capacity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept and safely queue up to 100 valid inference requests per gateway process while preserving provider/proxy limits, cancellation, exact usage accounting, and real proxy routing.

**Architecture:** Add a process-local FIFO-ish `InferenceAdmission` gate before provider/proxy acquisition. Make provider capacity acquisition optionally wait with a deadline and notification/poll fallback, and classify proxy lease exhaustion separately so a provider slot is not pinned while waiting for a proxy. Expose bounded capacity metrics and configuration while leaving `/health` independent.

**Tech Stack:** Python 3.11+, asyncio, FastAPI/Starlette, SQLAlchemy async PostgreSQL, Pydantic Settings, pytest/pytest-asyncio, existing HTTP/proxy transport.

**Spec:** `docs/superpowers/specs/2026-09-27-bounded-admission-proxy-capacity-design.md`

## Global Constraints

- Default process-local in-flight capacity is `100`; queue memory is bounded and request 101 receives safe `gateway_busy` HTTP 429 with `Retry-After`.
- The request deadline remains authoritative; no waiter may outlive its request or hold a database session, provider admission, proxy lease, or account lock while queued.
- Provider/credential RPM, cooldown, and active-concurrency limits remain authoritative; the first rollout keeps the live provider limit at 16.
- Static HTTP proxy endpoints and Kiot key/endpoint pairs remain single-owner in the first rollout; direct fallback is never implicit.
- Quota holds are released as `not_dispatched` on pre-dispatch queue expiry/cancellation; actual input/cached/output/reasoning usage is settled exactly once after real provider responses.
- No OAuth, model catalog, token pricing, or provider credential behavior changes are included.
- The final acceptance check must use the configured 20 real proxies and real upstream responses; mock/lab output cannot be reported as the 100-user result.

## Review Focus

- Disconnect while queued: the waiter is removed once, quota is released, and no permit leaks (Task 1/3 tests).
- Provider slot released while proxy is busy: a request wakes without a held SQL transaction and no active admission row leaks (Task 2/4 tests).
- Permanent provider/proxy errors versus local capacity exhaustion: only capacity waits/retry; invalid credentials/proxies fail immediately (Task 2/4 tests).
- Streaming/WebSocket/compact cleanup: the gate permit lasts through stream close and is released on cancellation without changing usage semantics (Task 3/5 integration tests).
- Queue saturation and observability: exactly 100 valid requests can be admitted, request 101 gets `gateway_busy`, and health/admin remain responsive (Task 1/5/6 live checks).

### Task 1: Implement the process-local bounded inference gate

**Files:**
- Create: `autobuild_json/gateway/admission.py`
- Test: `tests/gateway/unit/test_admission.py`

**Interfaces:**
- Produces `InferenceAdmission(capacity: int = 100, metrics: optional callback = None)`.
- Produces `async with admission.enter(deadline: datetime) as ticket:` where `ticket.wait_ms` is the measured queue wait and `ticket.release()` is idempotent.
- Produces `admission.snapshot() -> dict` with `capacity`, `inflight`, `queued`, `accepted`, `queue_full`, `expired`, `cancelled`, `wait_p50_ms`, `wait_p95_ms`, and `last_wait_ms`.
- Raises `GatewayError("gateway_busy", 429, "request", retry_after=1)` when the combined waiting/running count reaches capacity; raises `deadline_exceeded` when a queued request reaches its deadline.

- [ ] **Step 1: Write failing tests** for FIFO-ish wake-up, capacity 2 with a third request getting `gateway_busy`, deadline expiry removing a waiter, cancellation removing a waiter, idempotent release, and snapshot counters/wait percentiles.
- [ ] **Step 2: Run the focused tests and verify RED** with `pytest tests/gateway/unit/test_admission.py -q`; expected failure because the module/interface is absent.
- [ ] **Step 3: Implement `InferenceAdmission`** with an asyncio condition/deque, combined queued+running bound, monotonic wait measurement, cancellation-safe waiter removal, and idempotent tickets. Do not use a bare semaphore because it cannot enforce the combined queue bound or expose FIFO metrics.
- [ ] **Step 4: Run focused tests and verify GREEN** with `pytest tests/gateway/unit/test_admission.py -q`.
- [ ] **Step 5: Commit** `feat: add bounded inference admission gate`.

### Task 2: Add deadline-aware provider admission waiting

**Files:**
- Modify: `autobuild_json/gateway/providers/limits.py`
- Test: `tests/gateway/integration/test_provider_admission_wait.py`
- Test: `tests/gateway/integration/test_review_regressions.py` (preserve existing non-waiting behavior)

**Interfaces:**
- Extends `ProviderLimits.acquire(route, deadline, *, wait=False)`; current callers retain `wait=False` behavior.
- `wait=True` waits for local active capacity and returns the same admission identity/context manager; it never waits for disabled records, cooldown, RPM rejection, or permanent configuration errors.
- Produces `ProviderLimits.snapshot()` only if needed by metrics wiring; no raw provider/credential identifiers are exposed.

- [ ] **Step 1: Write failing PostgreSQL integration tests** for a concurrency-1 provider where the second `wait=True` acquisition waits until the first releases, a deadline expires without a leaked row, and `wait=False` still raises the existing 503 immediately.
- [ ] **Step 2: Run the focused integration tests and verify RED** with `pytest tests/gateway/integration/test_provider_admission_wait.py -q`; expected failure because `wait` is unsupported.
- [ ] **Step 3: Implement wait mode** using short committed transactions and an in-process release event/condition plus a bounded 50 ms fallback poll. Do not hold a SQLAlchemy session/row lock while sleeping. Notify waiters in the `finally` release path. Count wait attempts/expired waits in private counters.
- [ ] **Step 4: Run focused and existing provider tests** with `pytest tests/gateway/integration/test_provider_admission_wait.py tests/gateway/integration/test_review_regressions.py -q`.
- [ ] **Step 5: Commit** `feat: wait for provider capacity without holding db sessions`.

### Task 3: Wire admission into Engine with exact cleanup

**Files:**
- Modify: `autobuild_json/gateway/engine.py`
- Modify: `autobuild_json/gateway/admin/services.py`
- Modify: `autobuild_json/gateway/settings.py`
- Test: `tests/gateway/unit/test_engine_admission.py`
- Test: `tests/gateway/integration/test_codex_gateway.py`

**Interfaces:**
- `ServiceSettings.inference_capacity: int = Field(default=100, ge=1, le=10_000)`.
- `AdminServices` constructs one `InferenceAdmission(settings.inference_capacity)` and passes it to `Engine`.
- `Engine.__init__(..., admission=None)` defaults to a capacity-100 gate for compatibility harnesses.
- `Engine.prepare()` enters the gate after route/policy ledger reservation and before `route_resources`; the resulting prepared call owns the ticket through stream/non-stream collection and closes it in all exit paths.
- `Engine.capacity_snapshot() -> dict` returns gate plus provider/proxy wait counters for admin/status wiring.

- [ ] **Step 1: Write failing tests** proving a prepared request holds a gate ticket until `PreparedCall.collect()`/stream close, queue expiry releases an unspent ledger hold, cancellation during preparation releases the ticket, and a direct `Engine` constructed by existing test harnesses still has capacity 100.
- [ ] **Step 2: Run focused tests and verify RED** with `pytest tests/gateway/unit/test_engine_admission.py -q`.
- [ ] **Step 3: Implement constructor/config wiring and `AsyncExitStack` ownership**. Ensure the gate is released before final cleanup returns, but after provider/proxy/stream resources and ledger settlement have run. Do not change retry behavior for ambiguous upstream streams.
- [ ] **Step 4: Run focused engine and Codex integration tests** with `pytest tests/gateway/unit/test_engine_admission.py tests/gateway/integration/test_codex_gateway.py -q`.
- [ ] **Step 5: Commit** `feat: bound inference requests at engine admission`.

### Task 4: Classify and retry proxy capacity safely

**Files:**
- Modify: `autobuild_json/gateway/proxy/manager.py`
- Modify: `autobuild_json/gateway/engine.py`
- Test: `tests/gateway/unit/test_proxy_manager.py`
- Test: `tests/gateway/unit/test_engine_resource_order.py`

**Interfaces:**
- Adds an internal `proxy_capacity` signal/code for an unclaimed local lease that is safe to retry; public errors remain in `SAFE_CODES` and do not expose endpoint URLs.
- `ProxyManager.acquire(..., wait=True)` waits up to the request deadline for a lease and raises permanent `proxy_not_ready`, `kiot_key_invalid`, or `kiot_unavailable` immediately as appropriate.
- `Engine.route_resources()` calls provider wait mode and ensures provider admission is exited before retrying proxy capacity; it never holds a provider slot while sleeping for proxy availability.

- [ ] **Step 1: Write failing unit tests** for static pool capacity waiting, permanent disabled proxy failure, Kiot key invalidation, provider slot release before proxy retry, and cancellation cleanup of all lease tokens.
- [ ] **Step 2: Run focused tests and verify RED** with `pytest tests/gateway/unit/test_proxy_manager.py tests/gateway/unit/test_engine_resource_order.py -q`.
- [ ] **Step 3: Implement capacity classification and bounded condition/poll waiting**. Preserve endpoint exclusivity, Kiot rotation semantics, 10-second current acquisition cap unless the overall request deadline is shorter, and no direct fallback.
- [ ] **Step 4: Run focused proxy/resource tests and relevant quota tests** with `pytest tests/gateway/unit/test_proxy_manager.py tests/gateway/unit/test_engine_resource_order.py tests/gateway/integration/test_codex_quota.py -q`.
- [ ] **Step 5: Commit** `feat: wait and retry local proxy capacity safely`.

### Task 5: Expose configuration and service capacity metrics

**Files:**
- Modify: `autobuild_json/gateway/admin/codex_accounts.py` or the existing `ServiceStatus` implementation found during execution
- Modify: `autobuild_json/gateway/http/app.py`
- Modify: `autobuild_json/gateway/errors.py`
- Modify: `autobuild_json/static/index.html` and/or `autobuild_json/gateway/admin/static/codex-view.js` only if the existing status card has a stable capacity section
- Test: `tests/gateway/integration/test_codex_service_status.py`
- Test: `tests/gateway/http/test_boundary.py` or the closest existing boundary test file

**Interfaces:**
- `gateway_busy` is added to `SAFE_CODES`; OpenAI-shaped responses include `Retry-After: 1`.
- `/health` continues returning only readiness and does not enter the inference gate.
- Admin Codex service status gains a redacted `capacity` object with gate counters and wait percentiles; no prompts, keys, emails, credential IDs, or proxy URLs.
- Environment variable `AUTOBUILD_GATEWAY_INFERENCE_CAPACITY` configures the gate through `ServiceSettings`.

- [ ] **Step 1: Write failing tests** for config bounds, `gateway_busy` response envelope/header, status capacity payload, and health responsiveness while a gate ticket is held.
- [ ] **Step 2: Run focused tests and verify RED** with `pytest tests/gateway/integration/test_codex_service_status.py tests/gateway/http/test_boundary.py -q` (adjust only the boundary test path if the repository uses another existing file).
- [ ] **Step 3: Implement redacted status/error wiring** and minimal UI display only where an existing Codex status view already renders service health. Keep API compatibility and do not expose provider internals.
- [ ] **Step 4: Run focused plus settings tests** with `pytest tests/gateway/integration/test_codex_service_status.py tests/gateway/unit/test_service_setup.py tests/gateway/http/test_boundary.py -q`.
- [ ] **Step 5: Commit** `feat: expose gateway capacity metrics and queue errors`.

### Task 6: Run full verification and real 100-user proxy load

**Files:**
- Modify: `data/proxy_load_verify.py` only as an ignored operational benchmark if required to record queue metrics; never add credentials or artifacts to git
- Create: `docs/research/2026-09-27-bounded-admission-live-verification.md`

**Interfaces:**
- The benchmark must use 100 distinct temporary API keys, the configured `gpt-6-astra` route, the configured 20 HTTP proxies, real `/v1/responses`, and cleanup/revocation in `finally`.
- The report records status/error stage, actual usage fields, queue/inflight metrics, health latency, DB connection/lock observations, proxy probe results, and cleanup counts without secrets.

- [ ] **Step 1: Run the full offline suite** with `.venv/bin/python -m pytest -q`, plus `ruff check autobuild_json tests` and `.venv/bin/python -m compileall -q autobuild_json`; record exact counts/failures before live work.
- [ ] **Step 2: Restart the user gateway with `AUTOBUILD_GATEWAY_INFERENCE_CAPACITY=100`** while preserving the configured proxy profile and provider limit; verify `/health` and admin status.
- [ ] **Step 3: Run one real non-streaming and one real streaming request** through the configured public listener and verify non-empty response, usage fields, persisted ledger settlement, and zero active admission/lease leaks.
- [ ] **Step 4: Run `.venv/bin/python -u data/proxy_load_verify.py burst`** with no client retry for the acceptance measurement; then run the bounded workflow mode only as a secondary latency observation. Do not call any 503-free result a guarantee if the request deadline expires.
- [ ] **Step 5: Write the live report** separating accepted/queued/completed/expired, genuine upstream errors, queue p50/p95, end-to-end p50/p95, health max latency, provider active maximum, proxy lease maximum, DB connection/lock maximum, usage totals, and cleanup verification.
- [ ] **Step 6: Commit only the redacted report and operational code changes** with `docs: record bounded admission live verification`.

## Self-review checklist

- Spec coverage: gate, provider waiting, proxy retry classification, status, accounting, cancellation, live verification, and rollback are covered by Tasks 1–6.
- Interface consistency: `InferenceAdmission` is constructed by services and accepted by `Engine`; Tasks 1 and 3 define the ownership contract before Tasks 4–5 consume it.
- No task changes provider limits, OAuth, model routing, token rates, or direct-fallback policy.
- The plan distinguishes source/unit evidence from the required real 100-user response evidence.
- The only unresolved product decision is whether the user wants 100 accepted queued requests (this plan) versus 100 simultaneous upstream generations; the live limit of 16 makes the latter impossible without a separately approved provider/proxy capacity change.
