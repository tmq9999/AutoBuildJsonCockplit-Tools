# Antigravity frontend functional contract and deployment runbook

This document is the handoff contract for an Antigravity implementation of the
private gateway frontend. It describes behavior, data, API boundaries,
security, verification, and operations only.

## Non-goals and design freedom

This document does **not** prescribe colors, typography, spacing, component
names, page arrangement, navigation placement, animations, icons, or a visual
design system. Antigravity may choose the framework, component model,
information hierarchy, responsive strategy, and visual language.

The implementation is accepted only when the functional and security
requirements below are met. A visual choice is acceptable when it does not
hide an action, misrepresent a state, leak a secret, or make a required flow
unusable on a supported viewport.

## Product boundary

The frontend is an authenticated operator console for:

1. gateway service status, usage, reports, and charts;
2. customers and customer API keys;
3. Codex OAuth accounts, quota, reset credits, account pools, and model
   capabilities;
4. providers, provider credentials, models, bindings, aliases, and budgets;
5. proxy profiles and the Codex default proxy;
6. OAuth JSON import/refresh;
7. a real gateway API playground; and
8. audit and operational diagnostics.

The public gateway API is a separate origin/listener in production. The admin
console must never use an admin cookie or admin token as a customer API key.

## Functional contract

### 1. Session and request boundary

- Login accepts the private admin token through the existing `/api/session`
  contract. Store the session in an `HttpOnly` cookie supplied by the server.
- Read the returned CSRF token in memory and send it on every mutation.
- Send same-origin requests with credentials. Do not put admin tokens, CSRF
  tokens, customer keys, OAuth tokens, proxy credentials, or provider keys in
  localStorage, sessionStorage, URL parameters, analytics, or logs.
- A mutation must include the matching `Origin`; a missing/foreign origin or
  missing CSRF token is an expected rejected state, not a reason to retry.
- On `401`, clear in-memory state and require login again. On `409`, reload the
  authoritative record and explain that the editor was stale. Do not overwrite
  a newer server version.
- Late responses from a previous account, filter, tab, or logged-out session
  must not replace current state.
- Every loading, empty, success, rejected, unavailable, and ambiguous state
  needs an explicit user-readable message and a retry/reload path where safe.

### 2. Gateway overview and charts

Use the stored server facts from:

- `GET /api/service/codex-service/status`
- `GET /api/service/overview`
- `GET /api/service/usage`
- `GET /api/service/usage/requests`
- `GET /api/service/usage/accounts`

The overview must show, using any suitable chart or tabular equivalent:

- requests, completed, failed, pending/usage-pending;
- input, cached-read, cached-write, output, reasoning, and total tokens;
- weighted quota charged and quota held;
- active/disabled/reauth/unverified Codex account counts;
- enabled customer-key counts; and
- the selected time range and filters.

Chart behavior is functional, not visual: totals must come from the server
report over the complete requested range, not from the currently displayed
page. A trend may use paginated items only when it is explicitly labeled as a
page-level trend. Unknown usage remains unknown; it must not render as zero.
Cached tokens are already part of input and reasoning is already part of
output; neither may be added a second time.

### 3. Customers and API keys

Customers:

- `GET /api/service/customers`
- `POST /api/service/customers`
- `PATCH /api/service/customers/{id}`
- `DELETE /api/service/customers/{id}`

The delete operation is a soft disable. It preserves API keys, usage, audit,
and history. A disabled customer cannot issue or use keys. The console must
provide a clear path to re-enable the customer with the current `version`.
When key creation receives `permission_denied` because the customer is
disabled, identify the cause and link the operator to re-enable that customer;
do not silently remove the customer from the selector or make the action appear
to be a network failure.

API keys:

- `GET /api/service/keys` and `GET /api/service/key-balances`
- `POST /api/service/keys`
- `PATCH /api/service/keys/{id}`
- `POST /api/service/keys/{id}/rotate`
- `POST /api/service/keys/{id}/revoke`
- `DELETE /api/service/keys/{id}`
- `POST /api/service/keys/{id}/quota-adjust`

Required behavior:

- A newly issued or rotated secret is shown exactly once, then removed from
  the DOM when the operator closes its disclosure.
- Quota input/output/cache coefficients and quota limits use exact decimal
  string/micro-unit conversion; JavaScript `Number` is not authoritative.
- The UI distinguishes enabled, disabled, expired, and revoked keys. “Delete”
  and “revoke” are append-only invalidation operations; they do not erase
  usage or audit records.
- Disable a destructive button only when the current state or an in-flight
  mutation makes it unsafe. State the reason. A permanently disabled delete
  control with no explanation is not acceptable.
- A quota adjustment requires amount, reason, current version, and an explicit
  confirmation. Definite `4xx` rejection may reload and become retryable;
  ambiguous transport failure must not automatically resend a POST.

### 4. Codex OAuth accounts and quota

Use the server-side paginated account contract:

- `GET /api/service/oauth-accounts?limit=&page=&q=&status=`
- `PATCH /api/service/oauth-accounts/{id}`
- `GET /api/service/oauth-accounts/{id}/quota`
- `POST /api/service/oauth-accounts/{id}/quota/refresh`
- `GET /api/service/oauth-accounts/{id}/reset-credits`
- `POST /api/service/oauth-accounts/{id}/reset-credits/consume`
- `POST /api/service/reset-requests/{id}/resolve`
- `GET /api/service/codex-service/quota-refresh`
- `PUT /api/service/codex-service/quota-refresh/settings`
- `POST /api/service/codex-service/quota-refresh`
- `POST /api/service/codex-service/quota-refresh/stop`

The account experience must support:

- full email identity in the authenticated private console;
- server-side search, status filtering, numbered pagination, page-size
  selection, and a page jump without loading 1,000 accounts into one DOM;
- per-account enable/disable and proxy-profile assignment using CAS version;
- stored quota snapshots with freshness, reset times, plan, normal quota, and
  additional model/reserve quota; missing/stale values display as unknown;
- per-account quota refresh, with no hidden refresh while merely viewing data;
- a server-side “refresh all enabled accounts” job with progress, success,
  failure, skipped, rate-limit, stop, and reconnect states;
- persisted automatic-refresh interval where `0` means disabled; and
- reset-credit confirmation that never resends an ambiguous request. States
  such as `prepared`, `dispatched`, `unknown`, and
  `succeeded_refresh_failed` remain locked until an explicit authoritative
  refresh or audited resolution.

### 5. Models, providers, bindings, and catalog

Support the following resource flows and preserve their `version` values:

- providers: list/create/update/disable, discovery, credentials, credential
  enable/disable and rotation;
- models: list/create/update/soft-disable, exact input/output/cache rates;
- bindings: list/create/update with provider, credential, public model, upstream
  model, capabilities, bounds, priority, and enabled state;
- aliases and upstream budgets;
- Codex catalog/capabilities and explicit publish/review operations; and
- account pools per canonical model, including mode, members, priority/weight,
  single-account selection, freshness limits, retry limit, reserve settings,
  and CAS conflict recovery.

Unsupported capabilities must be labeled unsupported and must not be rendered
as successful configuration. Catalog visibility is not proof that an upstream
account is entitled to use a model.

### 6. Proxy profiles and saved configuration

Proxy profile APIs:

- `GET|POST /api/service/proxies`
- `PUT /api/service/proxies/{id}/{version}`
- `DELETE /api/service/proxies/{id}`
- `POST /api/service/proxies/{id}/release`
- `GET|PUT /api/service/codex-service/proxy`

Support direct, fixed, pool, and KiotProxy profiles with region, protocol,
rotation, and one-entry-per-line input. Proxy entries are write-only:

- never display the saved secret or echo it in errors;
- display the saved profile identity, mode, region, protocol, version, and
  reference/health state;
- when editing an existing profile, a blank secret field means “preserve the
  encrypted entries”, not “replace with empty”; entering a replacement is an
  explicit operation;
- after save, re-read or apply the server receipt and show the persisted
  profile, rather than clearing the selection; and
- if a profile is referenced by a provider/account, explain why deletion is
  blocked and identify the references. Do not silently delete or fall back to
  direct egress.

The Codex default proxy selector must round-trip the provider version and
profile ID. A stale version requires a reload before another save. Kiot release
must be an explicit operator action and must not release a lease used by a live
  request.

### 7. OAuth import and account maintenance

Support single and batch JSON import through:

- `POST /api/service/oauth/import`
- `POST /api/service/oauth/import-batch`
- `POST /api/service/oauth/{id}/refresh`

Show imported/duplicate/failed counts and per-record safe error codes without
rendering access tokens, refresh tokens, ID tokens, passwords, or raw records.
Import is not an implicit login and does not silently call upstream providers.

### 8. Real API playground and reports/audit

- Playground requests must use the same gateway engine, provider routing,
  proxy, pool, policy, and quota ledger as a public request.
- It must support the protocols the backend advertises and show the actual
  response, usage, charge, pending state, and safe error code. It must not use
  fabricated “smoke” responses.
- Reports support requests/accounts, time range, model, account, pagination,
  unknown usage, held quota, weighted charge, and upstream cost as separate
  concepts.
- Audit entries are read-only and must never include secrets or raw provider
  bodies.

### 9. Accessibility and resilience acceptance

Antigravity chooses the presentation, but the result must:

- work with keyboard-only interaction and a screen reader for every action;
- expose names, status, progress, errors, and confirmation requirements to
  assistive technology;
- remain usable at narrow and wide supported viewports without hiding required
  actions or introducing horizontal document overflow;
- avoid duplicate submissions while a mutation is pending;
- retain form drafts when a dialog/view is closed and reopened unless the
  operator explicitly discards them; and
- expose an explicit reload path after a stale session, failed page load, or
  ambiguous mutation.

## Deployment runbook

### Prerequisites

- A reviewed commit containing the frontend assets and corresponding contract
  tests.
- Python 3.10+, the project `.venv`, and Node dependencies from the locked
  `package-lock.json` when browser tests are run.
- A dedicated staging PostgreSQL/keyring/data directory. Never use production
  data for browser fixtures.
- Chrome/Chromium for Playwright tests (`CHROME_PATH` may point to it).
- An operator-provided admin token. Do not put it in CI logs, screenshots,
  issue text, or shell history.

### Pre-deployment checks

From the repository root:

```bash
.venv/bin/pytest -m 'not postgres'
npm run test:ui
npm run test:codex-layout
npm run test:codex-pagination
.venv/bin/ruff check autobuild_json tests
.venv/bin/python -m compileall -q autobuild_json
git diff --check
```

If the change touches backend contracts, also run the gateway integration suite
with its dedicated PostgreSQL fixture:

```bash
.venv/bin/pytest tests/gateway
```

Do not call a build successful when a test was skipped due to a missing browser,
database, or service; record the skip and run it in staging.

### Staging deployment

1. Confirm the target commit and save the current frontend asset directory and
   configuration backup outside the repository. Do not overwrite the backup.
2. Install the locked dependencies in the staging virtual environment.
3. Apply database migrations only when the commit contains backend migration
   changes; a frontend-only asset update does not require a migration.
4. Start the private admin listener against the staging database/keyring. Keep
   the public gateway and admin listener on their configured, separate origins.
5. Serve the frontend from the same origin as `/service/` and
   `/api/service/*`. If Antigravity uses a bundler, copy only its reviewed
   production output into the package's admin static asset directory and keep
   source maps/private source outside the public listener unless intentionally
   protected.
6. Open `/service/`, log in with the staging admin token, and verify that all
   asset requests are same-origin and that the browser console has no errors.
7. Run the browser smoke suite against staging. Use disposable customers,
   keys, providers, profiles, and OAuth fixtures; revoke/disable them during
   cleanup and retain only safe audit evidence.
8. Verify the functional checklist below before promoting the commit.

### Staging functional checklist

- Login, logout, session expiry, CSRF rejection, and foreign-origin rejection.
- Customer create, rename, soft-delete/disable, re-enable, and blocked-key
  creation with an actionable `permission_denied` explanation.
- API-key create, one-time secret cleanup, exact quota display, edit, toggle,
  rotate, revoke/delete, and quota adjustment.
- Account search/filter/pagination at 1,000 records; detail quota snapshot;
  refresh one; refresh all; stop; automatic interval save; reset credit
  uncertain-state recovery.
- Model/provider/credential/binding/catalog/pool flows with a stale-version
  conflict and a safe retry.
- Proxy profile create/update/delete/reference blocking; blank write-only edit
  preserves entries; Codex default proxy save round-trips; Kiot release is
  explicit.
- OAuth single/batch import reports safe counts and never renders a token.
- Overview numbers and charts match server reports for a known date range;
  pending and unknown usage remain distinct.
- Playground sends a real request only when intentionally enabled and displays
  its actual response/usage/error.
- Keyboard navigation, assistive labels, narrow viewport, page-load failure,
  stale response, and logout during an in-flight request.

### Promotion and rollback

Promotion is an asset/version change, not a database reset. Record the commit,
asset checksum, API contract version, test commands, and operator/time window.

To roll back a frontend-only release:

1. stop or drain the admin listener according to the deployment supervisor;
2. restore the previously reviewed static assets from the protected backup or
   previous commit;
3. restart the admin listener; and
4. re-run the session, customer/key, proxy-save, and chart smoke checks.

Do not roll back migrations or delete database records to undo a frontend bug.
If a frontend release exposed a backend contract defect, disable the affected
action, preserve audit data, and deploy a coordinated backend fix.

### Incident handling

- `401`: session expired or invalid; log in again, do not retry the mutation.
- `403`: origin/CSRF/policy denial; inspect the request boundary and explain
  the cause without exposing the token.
- `409`: stale version or in-use resource; reload authoritative state and ask
  the operator to review before saving again.
- `422`: invalid input; show field-level guidance without echoing secrets.
- `429`: provider/gateway rate limit; show retry timing and do not fan out
  duplicate refresh/reset requests.
- `5xx`/network timeout after a POST: treat the outcome as ambiguous unless the
  server proves it was never dispatched. Do not automatically resend or release
  quota.

Escalation evidence should contain timestamp, route, HTTP status, safe error
code/stage, request ID if available, and the affected non-secret resource ID.
It must not contain cookies, API keys, OAuth tokens, proxy URLs with userinfo,
or raw upstream response bodies.

## Appendix: Implementation and Verification Evidence (2026-09-28)

### Backend gaps identified and resolved

| Yêu cầu / Quy trình | Endpoint | Hành vi ban đầu | Khắc phục & Trạng thái |
| --- | --- | --- | --- |
| **Bảo toàn encrypted entries khi sửa proxy** | `PUT /api/service/proxies/{id}/{version}` | Báo lỗi hoặc xóa entries nếu form gửi `entries_text: ""` khi sửa metadata | **Đã sửa backend:** trong `autobuild_json/gateway/proxy/profiles.py` (`ProfileStore.update`), nếu `entries_text` trống và profile không phải `direct`, hệ thống nạp và giải mã ciphertext hiện có để kiểm tra tương thích mode/config mới và giữ nguyên `encrypted_entries`, tăng version CAS. Đã bổ sung integration test trong `tests/gateway/integration/test_admin.py` (12/12 pass). |
| **Khách hàng bị khóa khi cấp key** | `POST /api/service/keys` | Trả về 403 `permission_denied` chung chung | **Đã sửa UI:** Form cấp key hiển thị nhãn `[Đã khóa]` và inline warning khi chọn khách hàng bị khóa; khi submit nhận 403, UI bắt lỗi và giải thích rõ: `"Không thể cấp API key: Khách hàng [Tên] đang bị khóa. Cần mở lại khách hàng trước khi cấp key."` |
| **Xóa proxy đang được tham chiếu** | `DELETE /api/service/proxies/{id}` | Trả về 409 `invalid_state` | **Đã sửa UI:** Bắt lỗi 409 và giải thích rõ ràng nguyên nhân: `"Không thể xóa profile proxy: Profile này đang được gán cho Provider hoặc Tài khoản Codex. Vui lòng gỡ liên kết trước khi xóa."` |
| **Lọc bản ghi không còn hoạt động** | `GET /api/service/{resource}` | Không có bộ lọc trạng thái active/inactive trên giao diện danh sách | **Đã thêm UI:** Thêm `#status-filter` ("Tất cả", "Đang hoạt động", "Đã khóa / Thu hồi") trên thanh tiêu đề bảng điều khiển. |
| **Bảo mật account UUID trong DOM** | `GET /api/service/oauth-accounts` | DOM chứa trực tiếp account UUID trong options dropdown báo cáo | **Đã sửa UI:** Dùng opaque reference tokens (`acc_ref_X`) trong DOM options, mapping nội bộ gửi credential UUID thực sự lên API, khử sạch ID nhạy cảm khỏi DOM. |

### Verification test execution log

- **Node.js UI Unit Suite:**
  `npm run test:ui` -> **PASS (57/57 passed, 0 failures)**
- **Gateway Browser Smoke Suite:**
  `npm run test:gateway-browser` -> **PASS**
  (Kiểm thử: real login, customer/key CRUD, exact quota 100m, one-time secret modal, Kiot profile, XSS safety, mobile 390px, logout cleanup)
- **Codex Layout Smoke Suite:**
  `npm run test:codex-layout` -> **PASS**
  (Kiểm thử: 5 tabs desktop/mobile, key modal create/edit/toggle/rotate/delete, one-time secret cleanup, import draft, no provider writes)
- **Codex Pagination Smoke Suite:**
  `npm run test:codex-pagination` -> **PASS**
  (Kiểm thử: 1,000 accounts, 24/48/96 limits, first/previous/next/last/jump, search/status filters, failed-load recovery, late-response protection, mobile 390px)
- **Codex Browser & Regressions Suite:**
  `npm run test:codex-browser` -> **PASS**
  (Kiểm thử: toàn bộ 11 review regressions + main smoke pass)
- **Workbench Browser Smoke Suite:**
  `npm run test:browser` -> **PASS**
- **Python Unit Test Suite:**
  `.venv/bin/pytest -m 'not postgres'` -> **PASS (894 passed, 900 deselected)**
- **PostgreSQL Integration Suite:**
  `.venv/bin/pytest tests/gateway/integration/test_admin.py` -> **PASS (12/12 passed)**
- **Static Analysis & Whitespace:**
  - `.venv/bin/ruff check autobuild_json tests` -> **PASS (All checks passed)**
  - `.venv/bin/python -m compileall -q autobuild_json` -> **PASS**
  - `git diff --check` -> **PASS (0 whitespace errors)**

