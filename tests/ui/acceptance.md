# Local browser acceptance

Run `npm ci && npm run test:browser` after the Python dependencies are installed.
Set `CHROME_PATH` to a local Chromium/Chrome executable if it is not
`/usr/bin/google-chrome`; `TEST_PYTHON` overrides `.venv/bin/python`.

The script starts a temporary loopback-only FastAPI app with the real security,
runner, persistence and UI code. Only provider login/token exchange are synthetic.
The Python test server denies provider network; the browser aborts non-local requests.
No real account or user auth-cache token is used. Playwright is dev-only browser QA,
not the product OAuth transport (which remains HTTP).

Checks:

- Local login/cookie/CSRF; stale initial session lookup cannot undo a new login.
- Delayed history refresh cannot clear a just-accepted active batch; Stop stays
  enabled and status polling continues.
- UTF-8 file import, validation, filtered-row accounting, sequential/parallel options.
- Proxy batch with synthetic success/error/phone-verification results.
- Status filter and exact five JSON export downloads.
- Secret-bearing inputs cleared after accepted Start; no token/password in result DOM.
- Manual PKCE link/callback and JSON download; session cannot be reused in UI.
- Stop cancels pending and in-flight synthetic accounts; result counts remain accurate.
- Reload recovers authenticated history; logout clears fields and browser storage stays empty.
- Desktop 1440px and mobile 390px viewport, no document horizontal overflow.
- No page errors or external requests.

Screenshots are saved in ignored `.superpowers/browser-check/` for inspection.
This is not proof of live OpenAI login or network proxy connectivity.

## Gateway and Codex Accounts acceptance

Run sequentially: `npm run test:ui`, `npm run test:browser`,
`npm run test:gateway-browser`, then `node tests/ui/codex-browser-smoke.mjs`.
Gateway/Codex scripts use real private sessions/CSRF, stores and disposable PostgreSQL;
only upstream HTTP is synthetic. No real credential, provider/reset call or user data.

Codex Accounts follows approved draft v6 (Task11, including pool/grant follow-up fixes).
The smoke covers storage-only account reads; actual window durations/used and remaining
meters; one consume with durable succeeded_refresh_failed warning, reload, snapshot
recovery and separate evidence resolution; pool CAS409/reload; paginated member preservation;
empty/off-page single selection; exact 100m grant/audit; definite grant rejection/reload
versus lost successful receipt/no resend; cache zero/null, prefix/exclusions; DOM redaction;
keyboard/ARIA; desktop/mobile390; stale account/logout reads; no external browser network.

Customer weighted-token quota, upstream percentage windows and reset credits are separate.
Refreshing snapshots does not clear unknown/succeeded_refresh_failed operations or infer
success from percentages. Private `/api/service` routes are not public gateway routes.
HTTP text/tools/SSE is a tested subset, not full Codex CLI/Platform parity: compact, images,
WebSocket and profile takeover are absent. Provider Catalog UI is separate pending work.
These checks are synthetic acceptance, not live provider or deployment approval.
