# Implementation decisions and trade-offs

This is the exhaustive per-plan ruling record at the UI handoff; later entries supersede earlier checkpoint assumptions. Decisions are retained for review without including secrets or account data.

## Rate-limit follow-up — 2026-09-24

- User approved a bounded rate-limit/cooldown improvement after FreeLLMAPI
  comparison; no UI/schema/provider expansion. TDD reproduced adapter, chain,
  concurrent cooldown, count, cleanup-timing and continuation failures.
- Keep provider-wide cooldown and at most one different-provider fallback. No
  credential/IP rotation to evade limits, no ambiguous-generation/stream replay.
  Read Retry-After only from bounded standard headers, not free-form error bodies.
- Independent review identified cleanup restarting the deadline and omission of
  previously cooled routes. Regression tests reproduce both and concurrent longer
  cooldown; receipt-time deadlines and a fresh policy-scoped availability snapshot
  fix them. Follow-up also reproduced/fixed cooled continuation binding handling.
- Declined review suggestion to start cooldown after waiting for a database row
  lock: Retry-After starts at rejection receipt, not transaction commit. Time spent
  in cleanup/lock contention must count toward it; GREATEST preserves concurrent
  later deadlines. Restarting at commit would prolong the provider-stated delay.
- Retain known limits: synthetic provider/Kiot verification only, no production
  restart/deployment or real account operations. These changes do not assert
  additional resale rights or full vendor compatibility.

## Original gateway implementation rulings

- Ruling: “duyệt” approves the recommended Native execution and worktree setup in the written plan — no repeated approval prompt — cost if wrong: user can change execution method without losing work.

- Ruling: Task 1's PostgreSQL setup may build official PostgreSQL 17 binaries in ignored worktree-local dependencies, without system installation — no Docker/PostgreSQL is installed — cost if wrong: local build time and disk only, no host service changes.

- Task 1: Ruling: use test-only Zonky packaged PostgreSQL 17.6 from Maven Central after official source configure proved bison absent — SHA256 23da5a044b4fb7a5a081a45008c95749c873305328d73f86aefd56922ce1d29d verified; binaries ignored, not shipped — cost if wrong: test-only binary supply-chain exposure, no system service or real credentials.

- Task 1: Ruling: test fixture directly owns a random password-protected temporary PostgreSQL cluster when no explicit URL exists — easier reproducible isolated integration tests; original URL mode also requires dedicated loopback abgw_test database — cost if wrong: additional seconds per test session.

- Task 1: Ruling: test schemas use libpq/psycopg connections only after validated loopback test URL; Python outbound guard stays fully closed, including unrelated localhost ports — libpq bypasses Python socket methods — cost if wrong: native connector guarding depends on URL validation, addressed by explicit rejection tests.

- Task 1: Ruling: use build --no-isolation in the already isolated worktree venv because Python 3.14 has no ensurepip — default build failed before compilation — cost if wrong: packaging is verified against this venv, not a freshly pip-created build env.

- Task 1: Ruling: migrations take an explicit connected Database instead of an ini with a connection URL; test helper lifetime owns its temporary cluster without a persistent manifest — avoids persisted credentials and accidental independent migration target — cost if wrong: CLI must call migrate.upgrade explicitly (Task 18).

- Task 2: Ruling: client-key HMAC pepper must have at least 32 bytes; tests use 32-byte synthetic values rather than short plan examples — avoids accepting weak configured pepper — cost if wrong: old short test configs must be replaced.

- Task 4: Ruling: reservations and active slots are durable request rows instead of duplicate tables, with customer/key lock serializing all admissions/settlements — same accounting authority with fewer divergent states — cost if wrong: future queries must account for request state/deadline explicitly.

- Task 4: Ruling: provider monetary budgets use separate transaction/attempt holds after customer admission, with Engine compensating a failed budget admission by releasing the unspent customer hold — no HTTP before both holds exist — cost if wrong: crash between holds leaves a conservative pending hold for recovery, not overspend.

- Task 5: Ruling: aliases point only to canonical models (not alias chains), rejecting cycles structurally — simpler explicit model permission check — cost if wrong: operators must map alias directly to final model.

- Task 6: Ruling: use own HTTPX AsyncBaseTransport over httpcore pools with GuardedBackend, rather than mutating HTTPX private pool; httpcore AutoBackend is the only private import — enables connection-time IP pinning while preserving Host/SNI — cost if wrong: pin tested httpcore 1.x and adapt import on major upgrade.

- Task 7: Ruling: Kiot control client invokes HTTPX transport directly (not AsyncClient logging wrapper) to avoid logging query credentials; production uses guarded CoreTransport — no global logger suppression — cost if wrong: custom call wrapper must preserve timeout/close behavior, covered in client/manager tests.

- Task 7: Ruling: lease hard deadlines do not extend on renew; renew verifies ownership and heartbeat aborts at deadline/lost DB — makes reclaim after hard deadline+15s safe without indefinite lease extension — cost if wrong: long operations must request an adequate deadline at admission.

- Task 10: Ruling: ingress IP flood limiter is bounded process-local defensive guard; financially authoritative key RPM/concurrency remains PostgreSQL. Deployment reverse proxy must enforce global pre-auth limits across workers — avoids DB write per unauth request — cost if wrong: per-IP allowance scales with worker count until reverse-proxy rule applied.

- Task 10: Ruling: custom streaming ASGI response consumes generator in the same task that acquired proxy lease; disconnect watcher cancels that task — avoids context-owned heartbeat cancelling a different Starlette stream task — cost if wrong: maintain ASGI response lifecycle tests explicitly.

- Task 12: Ruling: same key RPM/concurrency authority is used for count and generation, a stricter combined limit rather than a second unconstrained counter — prevents free counter flooding while not charging generation tokens — cost if wrong: frequent counters reduce generation RPM available to the key.

- Task 15: Ruling: imports are staged unverified and deduplicated by issuer/account ID; explicit verified refresh promotes health. Avoid trust in expired/unverified JWT metadata — cost if wrong: first activation requires refresh rather than instantly using access token.

- Task 15: Ruling: Codex cannot honor explicit max_output_tokens, so Engine reserves certified model output bound without injecting a cap; requests with explicit unsupported cap fail before dispatch — cost if wrong: clients always sending caps cannot use that route until contract supports one.

- Task 16: Ruling: proxy lease encloses the HTTP attempt, while coordinator's source reservation remains through durable terminal write. Shared external lease releases when HTTP ends rather than after file write — no IP-sensitive HTTP remains at that point — cost if wrong: another worker may rotate after HTTP completes but before result file persistence.

- Ruling: continue independent Task18/19 backend operations and SDK checks while Task17 awaits required UI draft approval — plan explicitly permits backend progress; no implementation of unapproved UI — cost if wrong: task completion order differs, ledger is authoritative.

- Task 18: Ruling: encrypted logical backup limit 64 MiB; use pg row JSON under repeatable-read, restore to empty same-revision schema under exclusive table locks — avoids requiring pg_dump binaries in app deployment — cost if wrong: larger installations need a dedicated larger backup strategy before scale-up.

- Task 18: Ruling: keyring separates stable client_keys HMAC secret from active encryption key, to prevent encryption rotation from revoking client API keys — cost if wrong: keyring restore must preserve that additional key; backup tests check key identity.

- Ruling: monetary/weighted micro units use decimal strings on private admin wire to preserve values beyond JS safe integer; public inference token usage stays numeric — prevents rounding — cost if wrong: integrations to private admin must accept documented decimal strings.

- Ruling: production uses same approved split table/editor layout for entity tabs; detailed forms reflect actual API semantics (model permission shared across protocols, per-model coefficients) rather than illustrative per-protocol pricing blocks — spec is authority — cost if wrong: no separate per-protocol prices.

- Final rulings on declined judgments: live provider/Kiot correctness remains unverified without authorized live keys; remote proxy DNS requires operator firewall/trusted proxy; resale rights remain provider-contract responsibility. These are documented deployment limitations, not claimed test coverage. Visual fidelity assessed by main agent local Chrome screenshots against v3, not by code reviewer.

## Provider Catalog UI design gate — 2026-09-25

- Task 13 extends the saved Superdesign project `7d62e963-3633-45cd-ae16-6445cd1b4738`
  and draft `9da57406-2110-462f-a8ee-0f293bc9c332` in place. The draft is now
  version 4, titled **Provider Catalog | Dịch vụ API**, and keeps the existing
  matte dark-bronze Vietnamese admin shell, navigation, API-key screens and
  OAuth workbench unchanged.
- The incremental draft adds only catalog-specific surfaces: provider preset and
  source editor (credential, Direct/Fixed/Pool/KiotProxy effective proxy,
  schedule and worker status); paged catalog rows with added/changed/missing/
  ignored/unknown states, provenance and observed metadata; stale snapshot and
  429 retry notices; explicit publish preview with version conflicts,
  `all_models` warning and disabled-by-default new model/binding; separate
  read-only connection check and acknowledged costed probe with quote,
  16-token cap, pending/reconcile/error states; and queued/offline/running/
  cancelled/reconcile operation states. Synthetic labels only (`provider.invalid`,
  `model-demo`, `credential-demo`, `proxy-demo`) are present in the draft.
- The saved context bundle remains `.superdesign/design-system.md`,
  `autobuild_json/static/index.html`, and `autobuild_json/static/style.css`.
  The index fingerprint changed since the previous checkpoint and was refreshed
  after validating the target path, fingerprints and all six init artifacts.
  No secrets, account data, provider payloads, or production samples were sent.
- Visual approval is still required before Task 14 product UI implementation.
  The canvas/preview is the source of truth for that decision; backend Tasks
  1–12 remain usable for verification while the draft is reviewed.

## Codex API Service checkpoint — 2026-09-25

The user subsequently approved catalog draft v4 and the Codex API Service spec,
plan and subagent-driven execution. Catalog UI implementation remains separate;
its approval does not cover the newly added account/reset screens below.

Backend Tasks 1–9 are implemented through `e26e53f`, with independent task reviews:
exclusive input/cache-read/cache-write/output quota accounting, frozen rates,
durable account quota/reset state, four proxy modes, explicit reset-credit
consumption, pool routing/affinity, account-attributed attempts, model prefixes
and exclusions, private account/grant/report APIs. This is not a production
deployment or proof of live upstream availability.

Final observed backend verification: 1,307 tests passed on Python 3.14 with one
existing Google GenAI deprecation warning; Task 9 covering set passed 53 tests
on Python 3.10 and 3.14. Earlier full run interrupted after 87 passes; verbose
rerun passed, but the transient stall's root cause was not established. Final
whole-branch verification/review remains Task 12.

Task 9's deferred report-window boundary bug is fixed in Task12: authenticated
year-0001 default subtraction, positive-offset underflow and max-year UTC overflow
regressions reproduced unhandled OverflowError. A narrow catch translates invalid
normalization/arithmetic to the existing safe 422, without changing valid windows.

### Draft v6 approved; implemented account workspace

Superdesign draft `9da57406-2110-462f-a8ee-0f293bc9c332` is now version **6**, titled
**Codex Accounts | Dịch vụ API**. Version 5 was the single incremental generation;
version 6 applies deterministic corrections: no wildcard prefix, reset/grant
buttons disabled pending confirmation, fixed affinity TTL, truthful unsupported
capabilities and evidence-resolution copy. The progress ledger records the user's
2026-09-25 “triển khai đi” approval. Task11 implemented the workspace and its follow-up
pool/grant safety fixes through `61b4daa`. No new draft generation or deployment is claimed.

- Preview: https://p.superdesign.dev/draft/9da57406-2110-462f-a8ee-0f293bc9c332
- Canvas: https://superdesign.dev/teams/52a8e2c5-cbc2-465a-a07c-e51229258be3/projects/7d62e963-3633-45cd-ae16-6445cd1b4738
- Verified the saved same-draft version and corrected HTML after import. Local
  Chrome screenshots of the draft at 1440×1000 and 390×844 were inspected;
  document horizontal overflow was false and no page errors were emitted.
  These are static mockups, not evidence of production UI functionality.
- Existing catalog v4 remains in draft version history and its backend/UI work
  is separate. The account draft references that workspace rather than claiming
  it is newly implemented. At the Task10 design checkpoint product files were unchanged;
  Task11 subsequently implemented the account workspace. Provider Catalog UI remains pending.
- Only seven deliberately selected, path-validated UI source files were sent:
  `.superdesign/design-system.md` and gateway admin `index.html`, `style.css`,
  `app.js`, `forms.js`, `keys.js`, `dom.js`. No account/config/test/secret files.

### Task 10 account UI gate and endpoint mapping

This was the Task10 design gate; approval is now recorded and Task11 implements
these account/reset screens in local assets/native controls. All example identities,
metrics and profile names are synthetic. Provider Catalog UI remains separate pending work.

| UI surface | Private API under `/api/service` | Required state semantics |
|---|---|---|
| Masked account list/details | GET `oauth-accounts`; PATCH `oauth-accounts/{id}` | Versioned edit, no token/account-id/proxy-secret exposure; null override inherits provider |
| Quota/credit snapshot | GET `oauth-accounts/{id}/quota`, GET `oauth-accounts/{id}/reset-credits` | Storage-only GET; unknown/stale distinct from zero; actual window duration and timestamps |
| Explicit refresh | POST `oauth-accounts/{id}/quota/refresh` | Separate usage/credit outcome; preserve stale previous observations on partial failure |
| Reset confirmation | POST `oauth-accounts/{id}/reset-credits/consume` | Stable request UUID, current credits version and literal acknowledgement; one POST per confirmed operation |
| Reset resolution | POST `reset-requests/{id}/resolve` | Admin evidence/outcome/reason + version; unknown never auto-retried or inferred from percentage |
| Pool editor | GET/PUT `account-pools/{model_id:path}` | Canonical model; version 0 create-only; empty configured pool pauses routing |
| Customer quota grant | POST `keys/{id}/quota-adjust` | Increase total limit, preserve spent/held/history; idempotent receipt, no upstream reset |
| Usage and attempts | GET `usage/accounts`, GET `usage/requests` | Freeze returned time-window bounds during paging; serving-attempt charge once, unknown legacy attribution retained |
| Supported transports | GET `capabilities` | Tested HTTP text/tools subset; compact/images/WebSocket remain unsupported |

The UI must distinguish OpenAI percentage windows and provider-granted reset
credits from customer weighted-token quotas. All monetary/micro-token values
and aggregate counters use exact decimal strings, not JavaScript floats.

For the design example, inclusive input 1,000 with cached-read 600, cached-write
100 and output 200 leaves uncached input 300. Rates 1 / 0.1 / 1.25 / 3 charge
1,085 weighted tokens; a 100,000,000-token limit with zero held leaves
99,998,915. Granting 1,085 then raises the total limit to 100,001,085, preserving
the 1,085 spent and restoring 100,000,000 available. Blank cache rates inherit
the effective input rate; explicit zero is intentionally free.

Reset states are `prepared`, `dispatched`, `unknown`, `rejected`,
`succeeded_refresh_failed`, `succeeded`. Accepted-but-unverified reset must warn
and remain locked, not show a generic success or permit another consume.
Snapshot refresh and manual evidence resolution are distinct actions.

Operational limits: backend endpoints are reference-observed, not a stable
public OpenAI contract. Tests used synthetic HTTP and disposable PostgreSQL;
no real account/reset, production migration, restart, merge, push or deployment
was performed. Thread-backed OAuth cancellation remains cooperative; a
noncooperative native call can exceed the nominal cleanup budget. No raw
credentials or live account data were uploaded as design context.

## Task12 handoff — 2026-09-25

The new composed e2e uses existing synthetic identity/pool/ledger/PG/proxy/private-session
fixtures: 100m key, one same-provider account429, native upstream SSE with cached usage,
account reports, explicit admin refresh/reset, newer snapshot and storage-only UUID replay.
A dropped reset response remains unknown even after snapshot refresh; customer ledger is
unchanged by account admin operations. This is not live provider validation.

| Surface | Verified contract / limit |
|---|---|
| Chat/Responses HTTP | Text/function tools, JSON/SSE; exact `/backend-api/codex/responses` alias; no wildcard backend proxy |
| Codex options | Explicit output cap, temperature/top_p and unrepresented options rejected; certified bounds used for reservation |
| Compact/images | Authenticated HTTP unsupported_feature400; multipart rejected415, no generation reservation |
| WebSocket/profile takeover | WebSocket closes1008 before accept; no auth-cache/profile takeover |
| Account admin | Private session, same-origin/CSRF mutations; public listener404; GET snapshots storage-only |
| Quota tiers | Customer weighted-token ledger, upstream percentage windows, upstream reset credits distinct; monetary costs separate |
| Proxy | Credential override → provider profile → direct; direct/fixed/pool-list/Kiot are explicit modes, no failure fallback to direct |
| Reset recovery | Fresh positive credits/version/ack required; unknown/succeeded_refresh_failed locked; refresh is not resolution or consume |
| Grant/report | Grant only raises total; preserve spent/held/day/month; fixed returned report time bounds reused on paging |
| UI | Approved v6 Codex Accounts implemented; Provider Catalog UI still pending separately |

Fresh final checks: `.venv/bin/python -m pytest -q` **1312 passed**, one existing Google
GenAI warning (322.54s); `.deps/venv310/bin/python -m pytest -q` **1312 passed** (383.78s).
Both `.venv/bin/ruff check .` and `.deps/venv310/bin/ruff check .` passed.
`npm run test:ui` **34 passed**; `npm run test:browser`, `npm run test:gateway-browser`
and `node tests/ui/codex-browser-smoke.mjs` passed sequentially. Compileall and diff-check
passed. `.venv/bin/python -m build` is **blocked** by missing Python3.14 ensurepip;
`.venv/bin/python -m build --no-isolation` built sdist/wheel using installed dependencies.
No package installation or claim of successful isolated build. Controller owns independent
task/whole-branch review and integration choice; this task does not approve deployment.
