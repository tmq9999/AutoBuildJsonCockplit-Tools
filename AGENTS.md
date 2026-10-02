# AGENTS.md

## Project scope

`AutoBuildJsonCockplit-Tools` is a Python 3.10+ FastAPI application with two
related surfaces:

- the OAuth workbench (`/` on the private admin listener), and
- the opt-in multi-provider gateway plus its Vietnamese admin console
  (`/service/` and `/api/service/*`).

The gateway is an independently implemented codebase. It may use other gateway
projects as references, but do not copy their source, credentials, branding, or
assume that a reference project's behavior is supported here.

Instructions in this file apply to the whole repository. More specific
`AGENTS.md` files in a subdirectory take precedence for that subdirectory.
Existing `GEMINI.md` and `.agents/` instructions also apply.

## Repository map

- `autobuild_json/`: production Python package.
  - `api.py`, `security.py`, `oauth.py`, `runner.py`: OAuth workbench and
    private admin session boundary.
  - `gateway/http/`: public OpenAI/Anthropic/Gemini/Ollama/Codex HTTP and
    WebSocket routes.
  - `gateway/admin/`: private admin API, schemas, reports, and static UI.
  - `gateway/storage/`: PostgreSQL schema and Alembic-style migrations.
  - `gateway/identity/`, `metering/`, `routing/`, `providers/`, `proxy/`:
    identity/policy, quota accounting, account routing, upstream adapters, and
    proxy leasing.
- `tests/`: OAuth/workbench tests and gateway unit/integration tests.
- `tests/ui/`: Node/Playwright UI and live-verification scripts.
- `scripts/local_gateway.py`: private local runner that starts PostgreSQL,
  gateway API, and admin UI without creating synthetic production records.
- `docs/`: contracts, operations, compatibility notes, and verification
  evidence. Update the relevant document when behavior or an operator workflow
  changes.

## Required safety rules

1. Never commit or print admin tokens, API keys, OAuth access/refresh tokens,
   passwords, 2FA secrets, proxy credentials, KiotProxy keys, encrypted
   payloads, or raw provider response bodies. Redact logs and test output.
2. Treat `data/`, `account.txt`, `proxy*.txt`, local config/keyring files,
   exports, and `.worktrees/` as private operator state. Do not delete or reset
   them to make tests pass. Preserve unrelated working-tree changes.
3. Do not run destructive database commands against the local gateway data or a
   configured database. Use a dedicated test PostgreSQL instance/fixture for
   integration tests.
4. Admin mutations require the real session cookie, matching `Origin`, and
   `X-CSRF-Token`. Do not weaken those checks or add an admin bypass to a public
   endpoint.
5. Secrets are write-only in admin responses. Proxy profile entries and
   provider credentials are encrypted at rest and must not be returned by list
   endpoints or browser storage.
6. Do not claim a live provider/proxy/API capability from a mock, fixture, or
   synthetic smoke test. Report the exact command and evidence for live claims.

## Backend conventions

- Keep request handlers thin; put state transitions and locking in the relevant
  service (`identity`, `metering`, `routing`, `accounts`, `proxy`, or provider
  service).
- Use async SQLAlchemy transactions and PostgreSQL constraints/locks for
  authoritative state. Mutations that expose a `version` use compare-and-swap
  semantics and must return a conflict rather than overwrite a stale editor.
- Customer deletion is a soft disable so keys, usage, and audit history remain
  available. API-key deletion/revocation disables the key and preserves its
  ledger/audit records. Keep the UI wording consistent with these semantics.
- Represent token quota and coefficients as exact integer micro-units. Public
  JSON uses decimal strings where values may exceed JavaScript's safe integer
  range; never convert them through a floating-point number.
- Cached-read/write tokens are subdivisions of input and reasoning tokens are
  subdivisions of output. Validate the relationship and never count them twice.
  If upstream usage is absent or ambiguous, retain `usage_pending`/unknown
  accounting; never fabricate zero or silently refund a dispatched request.
- Public errors must use the gateway's safe error codes/stages and must not leak
  upstream bodies, account identifiers, email addresses where the contract says
  they are private, or proxy credentials.
- Add a migration for every schema change. Keep migrations forward-only and
  test upgrade behavior on a clean database.
- Proxy profiles support direct, fixed, pool, and KiotProxy modes. Kiot leases
  are exclusive and must not silently fall back to direct egress. Updating a
  write-only profile must not erase its encrypted entries merely because an edit
  form did not include a replacement secret.

## Frontend conventions

- The admin UI is plain ES modules under `autobuild_json/gateway/admin/static/`;
  there is no frontend build step. Keep modules small and use the existing DOM
  helpers (`dom.js`, `codex-ui.js`) rather than injecting unsanitized HTML.
- Keep API calls in `api.js`/the relevant feature module, preserve CSRF and
  same-origin credentials, and handle stale sessions/CAS conflicts explicitly.
- Show full operator-visible account identity only on the authenticated private
  console. Never expose upstream secrets or put them in `localStorage`.
- Use charts for aggregate usage trends, but label their time range and whether
  the data is a page or server-wide aggregate. Do not infer a total from a
  paginated page.
- Destructive-looking controls must say whether they soft-disable, revoke, or
  permanently remove something. Keep the action available when the stored state
  permits it; disable only for a documented reason and explain that reason in
  the UI.
- When a form edits a write-only value (proxy entries, provider credential, or
  key secret), leave the field blank and send a replacement only when the
  operator entered one. The saved value must remain selectable and visible via
  its non-secret name/mode/status.

## Local setup and runtime

Use a project virtual environment; do not modify the system Python:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,service,gateway-test]'
```

For a persistent local gateway, prefer the runner documented in
`docs/gateway-operations.md`:

```bash
.venv/bin/python scripts/local_gateway.py \
  --postgres-bin /absolute/path/to/postgres/bin
```

It owns the private PostgreSQL cluster and uses loopback ports 8787 (admin) and
8788 (public API). Read the admin token directly from the private local config
on the machine; never paste it into chat or a commit. Do not start a second
runner against the same `data/local-gateway` directory.

For a separately configured deployment, set
`AUTOBUILD_GATEWAY_DATABASE_URL` and
`AUTOBUILD_GATEWAY_MASTER_KEY_FILE`, run migrations, then start `serve`,
`admin`, and the single `maintenance` worker as described in the operations
guide. Keep the sum of database pool capacities below PostgreSQL's connection
budget.

## Verification workflow

Use test-first changes for behavior fixes: add one focused failing test, run it
to confirm the failure, implement the smallest fix, then run the focused test
and the complete relevant suite.

Typical checks:

```bash
# Python tests without a dedicated PostgreSQL fixture
.venv/bin/pytest -m 'not postgres'

# Full gateway suite when PostgreSQL test support is available
.venv/bin/pytest tests/gateway

# Static checks
.venv/bin/ruff check autobuild_json tests
.venv/bin/python -m compileall -q autobuild_json
git diff --check

# JavaScript/UI checks
npm run test:ui
npm run test:codex-layout
npm run test:codex-pagination
```

Browser/live scripts need the appropriate listener and private configuration;
run them only when the operator explicitly wants a real request. Live tests
must use a disposable customer/key/configuration and clean up by revoking or
disabling those records while retaining audit evidence. Do not substitute a
mock response for a requested live verification.

When diagnosing a failure, record the boundary where it occurs (browser →
admin API → service → database/provider/proxy), inspect the exact status/code,
and avoid bundling unrelated fixes. If a live request is ambiguous after it
may have reached an upstream, do not retry automatically or release quota
without evidence.

## Git and handoff

- Keep commits focused and describe the user-visible behavior or invariant.
- Do not use `git reset --hard`, broad checkout, or recursive deletion to clean
  up. Ask before discarding user changes.
- Before handing off, report changed files, tests run and their result, any live
  evidence, and known limitations. Never include secret-bearing output.
- Update `CHANGELOG.md` for user-visible behavior and the relevant `docs/` page
  for operator-facing configuration or API contract changes.
