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
