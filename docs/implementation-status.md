# Implementation status

## Implemented

- Tasks 1–8: strict account/proxy parsing, PKCE and single-use callback handling,
  signed token validation, pinned CheckLive HTTP integration, safe challenge
  classification, atomic result exports, bounded worker scheduling, protected
  FastAPI, manual callback APIs and approved local Vietnamese dashboard.
- Independent backend review completed; important findings for environment proxy
  interference, phone-error classification and disk-full recovery were reproduced
  by failing tests and fixed. Unicode password separators were likewise corrected.
- Actual local server startup, unauthorized API response and admin-session exchange
  checked with synthetic data. No real account login was performed.
- Whole-project independent review completed. Reproduced and fixed both Important
  findings: stale history responses could disable Stop/polling after Start; disk
  failure could leave unfinished rows indefinitely queued/running. Regression tests
  now cover current snapshot/history/exports and persistent disk failure.
- Final checks: 107 Python tests, 5 Node tests, and real Chrome acceptance passed.
  Ruff, compileall, wheel/sdist build and isolated installed-wheel UI smoke passed.
  Chrome acceptance covers login, file import/filter, parallel/proxy jobs, five
  export downloads, manual callback, Stop, reload/history, logout and 390px mobile.
  No external browser requests and no provider login occurred in these tests.
- Sentinel parity fix: the adapter no longer turns successful `turnstile.dx` metadata
  into an invented `ACTION_REQUIRED` before the pinned upstream fallback/optional VM
  path runs. Offline parity tests cover metadata, provider rejection, and CAPTCHA
  page branches. The optional VM is absent from the pinned checkout, so provider
  challenges requiring it remain unsupported.

## Known verification limits and deferred minor

- Live provider compatibility and Windows permissions/locking remain unverified.
  No user-selected real account has been run through OAuth.
- HTTP 429 is safely reported as RATE_LIMITED without automatic retry, but the
  provider's optional Retry-After duration is not yet displayed.
- Two upstream Starlette/TestClient deprecation warnings remain in the Python suite.

## Execution decisions

- New standalone project uses its own feature branch, not a second linked worktree;
  the reference repo remains untouched. Cost: no additional linked-worktree isolation.
- Commits use command-local `Codex <codex@localhost>` because no author was configured;
  no global Git configuration was changed. Cost: amend authorship if desired.
- The project venv was bootstrapped via system pip's `--python` support because the
  system lacked ensurepip. No system packages were installed. Cost: recreate local
  venv if its interpreter changes.
- Account rows split on CRLF/LF/CR, not all Unicode separators, to preserve passwords.
  Cost: input using Unicode row separators requires explicit conversion.

The public API's added history route and single-process filesystem lock support
restart recovery without changing the successful-export schema.

Reviewer scope decisions: live OAuth is not inferred from synthetic tests (cost:
provider changes may still require follow-up); Windows behavior is explicitly
unverified (cost: deployment there needs a local check). Final packaging/docs were
verified by the implementer. Approved Superdesign layout was implemented with local
assets and inspected through local Chrome screenshots, not a second remote design.
