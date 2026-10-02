# Antigravity Frontend Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a function-complete, secure, and verifiable private gateway frontend through Antigravity without prescribing its visual design.

**Architecture:** Antigravity owns the frontend implementation and may choose its framework and component structure. It consumes the existing same-origin private admin APIs, uses server-authoritative versions and reports, and keeps secrets only in memory during the necessary operation. The current repository remains the integration target: reviewed production assets are served from the admin static asset package and are verified with Node/Playwright tests.

**Tech Stack:** Existing FastAPI admin API, PostgreSQL-backed gateway contracts, ES-module/static asset packaging unless Antigravity supplies a reviewed build output, Node test runner, and Playwright browser verification.

**Spec:** `docs/runbooks/antigravity-frontend-deployment.md`

## Global Constraints

- No prescribed colors, typography, spacing, component names, page arrangement, navigation placement, animations, icons, or visual design system.
- Admin mutations require the server session cookie, matching `Origin`, and `X-CSRF-Token`.
- Secrets are write-only in API responses and are never stored in browser persistence or logs.
- Customer/key/proxy/provider mutations use server `version` compare-and-swap semantics.
- Customer deletion and API-key deletion/revocation preserve usage and audit history.
- Quota and coefficients use exact decimal strings/micro-units; unknown usage is never fabricated as zero.
- Ambiguous POST/network outcomes are not automatically retried.
- Charts must disclose range/filter and must not infer server totals from a paginated page.
- Production verification requires real API responses for live flows; fixtures are labeled as fixtures.

## Review Focus

- Disabled customer selected during key creation: the console explains `permission_denied` and offers re-enable, rather than hiding the customer or reporting a network error.
- Blank write-only proxy field during edit: the encrypted entries remain unchanged and the saved profile remains selectable.
- Ambiguous reset/refresh POST: the UI remains fail-closed and does not send a duplicate request.
- Large account catalog: pagination/search/filter operate server-side and late responses cannot replace newer state.
- Unknown/pending usage: charts, balances, and reports preserve unknown/pending status rather than showing zero.

---

## Work packages

### Task 1: Contract inventory and frontend shell

**Files:**
- Modify/create: Antigravity frontend source; integration output under `autobuild_json/gateway/admin/static/` when using the current package.
- Test: `tests/ui/gateway-state.test.mjs`, browser login/logout coverage.

**Interfaces:**
- Consumes: `/api/session`, `/api/service/*`, existing CSRF/session rules.
- Produces: one authenticated console session, request client, stale-session
  cancellation, safe error normalization, and logout cleanup.

- [x] Inventory every route in the runbook and map it to one frontend action.
- [x] Implement login, session restore, logout, expiry, CSRF, same-origin
  credentials, and no-persistence secret handling.
- [x] Add tests for `401`, `403`, stale response after logout, and non-JSON safe
  error handling.
- [x] Run `npm run test:ui` and the focused browser login/logout flow.
- [x] Commit the shell and contract tests independently.

### Task 2: Overview, usage reports, and functional charts

**Files:**
- Modify/create: overview/report modules and their tests.
- Test: `tests/ui/codex-report.test.mjs` plus a browser report check.

**Interfaces:**
- Consumes: service status, overview, usage summary, request/account reports.
- Produces: range/filter-aware totals, pending/unknown labels, and one or more
  charts whose visual treatment is chosen by Antigravity.

- [x] Implement server-range query state and report pagination.
- [x] Render total requests/tokens/charges from report summary, not page length.
- [x] Implement a trend with explicit range/page semantics and unknown handling.
- [x] Test cache/read/write/reasoning arithmetic and missing usage.
- [x] Verify no provider write occurs while viewing or changing report filters.
- [x] Commit only after the report and chart acceptance checks pass.

### Task 3: Customers and API-key lifecycle

**Files:**
- Modify/create: customer/key management modules and exact quota helpers.
- Test: `tests/ui/gateway-state.test.mjs`, gateway browser CRUD flow, and
  backend integration coverage where a contract gap is found.

**Interfaces:**
- Consumes: customers, keys, balances, rotate/revoke/delete, quota-adjust.
- Produces: actionable disabled-customer errors, one-time secret disclosure,
  exact quota forms, CAS-aware editing, and clear disable/revoke semantics.

- [x] Add customer create/rename/disable/re-enable and soft-delete behavior.
- [x] Add key create/edit/toggle/rotate/revoke/delete and balance display.
- [x] Keep disabled customer rows available for re-enable and explain why key
  creation is blocked.
- [x] Add exact micro-unit conversion tests beyond JavaScript safe integer range.
- [x] Add tests proving a revoked key cannot be edited/resubmitted as active and
  that its historical usage remains visible.
- [x] Commit customer/key behavior separately from visual styling.

### Task 4: Codex accounts, quota, reset, and pools

**Files:**
- Modify/create: account list/detail, refresh job, reset, and pool modules.
- Test: `tests/ui/codex-state.test.mjs`, `tests/ui/codex-pagination.test.mjs`,
  `tests/ui/codex-quota.test.mjs`, `tests/ui/codex-browser-smoke.mjs`, and
  `tests/ui/codex-pagination-smoke.mjs`.

**Interfaces:**
- Consumes: paginated OAuth account APIs, quota/reset APIs, refresh job APIs,
  capabilities, models, profiles, and account-pool APIs.
- Produces: server-side account catalog interaction, bulk refresh progress,
  fail-closed reset state, pool CAS editor, and per-account proxy selection.

- [x] Implement search/status/page-size/page-jump with only the current page in
  the DOM and stable credential IDs in report filters.
- [x] Implement per-account quota refresh, persisted auto-refresh settings,
  bulk refresh progress/stop, and reconnect/reload behavior.
- [x] Implement reset credit confirmation and all uncertain terminal states;
  never automatically resend an ambiguous consume request.
- [x] Implement pool mode/member/priority/weight/single-account controls and
  stale-version reload.
- [x] Test 1,000 accounts, late responses, stale pages, unknown quota, and
  reserve/additional quota rendering.
- [x] Commit account operations and pool behavior separately.

### Task 5: Providers, models, catalog, credentials, and proxies

**Files:**
- Modify/create: provider/model/binding/catalog/proxy/OAuth-import modules.
- Test: `tests/gateway/integration/test_admin.py`,
  `tests/gateway/integration/test_codex_admin.py`, browser admin smoke, and
  new proxy edit regression test if needed.

**Interfaces:**
- Consumes: provider/catalog/model/binding/credential/proxy/OAuth endpoints.
- Produces: versioned editors, write-only secret handling, reference-aware
  proxy lifecycle, and safe batch import results.

- [x] Implement provider/credential/model/binding/alias/budget lifecycle with
  version conflict recovery.
- [x] Implement proxy profile create/update/delete/reference checks, direct,
  fixed, pool, and KiotProxy modes, and explicit Kiot release.
- [x] Ensure blank write-only entries preserve the encrypted server value; if
  the backend contract currently rejects this, document and fix that contract
  before claiming the frontend flow complete.
- [x] Round-trip Codex default proxy selection using provider version and
  profile ID; never fall back to direct silently.
- [x] Implement OAuth single/batch import and safe result counts.
- [x] Commit resource lifecycle and proxy behavior separately.

### Task 6: Playground, audit, diagnostics, and accessibility

**Files:**
- Modify/create: playground, audit, diagnostics, and accessibility helpers.
- Test: live/fixture playground browser flow, keyboard checks, and no-secret
  DOM/log assertions.

**Interfaces:**
- Consumes: `/api/service/playground`, `/api/service/audit`, usage and status.
- Produces: real-response display, safe diagnostics, accessible status/error
  announcements, and usable keyboard/screen-reader flows.

- [x] Send playground requests only through the real gateway engine and show
  actual response/usage/error/pending outcomes.
- [x] Keep audit read-only and redact all secrets/provider bodies.
- [x] Verify keyboard operation, semantic names, focus recovery, narrow viewport,
  and draft retention without prescribing visual composition.
- [x] Commit diagnostics/accessibility tests independently.

### Task 7: Packaging, staging deployment, and release evidence

**Files:**
- Modify: package/static asset manifest only if the chosen frontend build needs it.
- Test/docs: update the runbook evidence and release checklist.

**Interfaces:**
- Consumes: reviewed frontend build and all prior test artifacts.
- Produces: same-origin `/service/` deployment, reproducible asset version,
  rollback path, and safe live/staging evidence.

- [x] Build using the locked dependency set and place only reviewed production
  assets in the package static directory.
- [x] Run Python, Node, browser, gateway integration, compile, and diff checks.
- [x] Deploy to a dedicated staging database/keyring and execute the complete
  checklist in `docs/runbooks/antigravity-frontend-deployment.md`.
- [x] Record commit, asset checksum, test results, skipped prerequisites, and
  known limitations without secret-bearing output.
- [x] Obtain operator review of the complete diff and evidence before promotion.

## Handoff gate

Antigravity implementation is ready for operator acceptance only when every
task has a passing focused test, the complete relevant suite is green, the
staging checklist is recorded, and no requirement was satisfied by silently
choosing a visual design or by replacing a real response with a mock claim.
