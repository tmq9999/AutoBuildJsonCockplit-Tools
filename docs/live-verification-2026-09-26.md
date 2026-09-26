# Live gateway verification — 2026-09-26

Requests went to the running gateway (`127.0.0.1:8788`) and real Codex upstream
using imported OAuth accounts. No mock responses or disposable database were
used for these results. Temporary customer keys were revoked after each run.
No account credentials or response reasoning payloads are included here.

## Observed failure and repair

Chat rejected `reasoning_effort` before dispatch; Messages rejected `thinking`.
Native Responses dispatched successfully, but a reasoning item containing
`content: []` was rejected by the local stream parser, so usage stayed pending.
The parser now accepts that empty field and retains only summaries/encrypted
replay data. Raw reasoning content is not exposed.

## Actual responses and exact ledger comparison

| Request | HTTP | Input | Cached | Output | Reasoning (within output) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Responses, Astra high | 200 | 19 | 0 | 14 | 7 |
| Chat Completions, Astra high | 200 | 19 | 0 | 17 | 10 |
| Messages, thinking enabled | 200 | 19 | 0 | 5 | 0 |
| Messages, adaptive high | 200 | 19 | 0 | 17 | 10 |
| Responses SSE, 5.6 Sol high | 200 | 19 | 0 | 5 | 0 |
| Chat SSE, Astra high | 200 | 19 | 0 | 14 | 7 |
| Private playground, Astra high | 200 | 12 | 0 | 7 | 0 |
| Cache probe 1, same session/prompt key | 200 | 3820 | 0 | 6 | 0 |
| Cache probe 2, same session/prompt key | 200 | 3820 | 0 | 6 | 0 |
| Cache probe 3, same session/prompt key | 200 | 3820 | 0 | 6 | 0 |

For each completed request, the request usage, serving attempt, and immutable
settlement agreed. At factor 1, the first row charged 33 tokens, not 40: reasoning
7 was already included in output 14. These calls do not demonstrate a cache hit;
the upstream reported zero cached and cache-write tokens.

`ultra` really returned HTTP 400 with `error.code=invalid_value` and
`error.param=reasoning.effort`. The returned enum was `none`, `minimal`, `low`,
`medium`, `high`, `xhigh`, `max`. Subsequent real Astra `max` and `xhigh` requests
completed (13 input / 27 output and 13 input / 21 output respectively). Gateway
now rejects `ultra` clearly before dispatch; it does not downgrade effort.

## Dashboard evidence

Immediately after the ten successful calls above, the live saved totals were:
11,829 input + 282 output = **12,111 total tokens**, including 72 reasoning
tokens within output. Cached and cache-write totals were zero. Requests total
37: 30 completed, 3 released/rejected, and 4 pending from earlier interrupted
diagnostics. These are a point-in-time observation, not hard-coded UI values.

Report requests with `limit=1` and `limit=100` returned the same server-side
summary. Chromium on the real admin page displayed the live totals, model
selector, and high-effort request JSON without JavaScript errors.

Known limitations: old requests whose provider usage was lost remain pending;
they are not counted as zero-token successes. No claim of successful Reserve
inference or positive provider cache hit is made by this verification.

## Continuation: real browser and account-filter repairs

Fresh real requests through the public gateway returned HTTP 200 and `API_OK`
for Responses high, Chat high, Messages enabled, Messages adaptive, Responses
SSE and Chat SSE. Each reported 10 input / 6 output / 0 cached tokens. A real
Chromium submission through the private playground returned 11 input / 6 output.
All seven request receipts matched their serving attempts and settlement rows:
113 tokens charged once, zero held, and **99,999,887** remaining on the temporary
100,000,000-token key at factor 1. Reasoning was zero for these short prompts;
earlier nonzero reasoning results above remain separate observations.

The first verification attempts exposed two harness mistakes, not provider
failures: an overly strict assertion required a trailing period, and the
Messages request omitted `anthropic-version: 2023-06-01`. The corrected requests
used the real endpoints and completed; neither failure was hidden or retried
as an upstream account failure.

Read-only review plus live reproduction found two product bugs:

- An account filter matched any retry attempt, then included the serving
  account's usage and charge. A real rejected-only account showed zero row
  charge but 33 tokens in its summary. The summary now attributes usage/charge
  to the unambiguous serving account, holds to the latest account, and rejected
  attempts to failures within the selected account's scope. After restarting,
  all six accounts involved in saved cross-account retries had matching report
  row and summary charges/holds; rejected-only accounts now show zero charge.
- First entry into Playground built its model selector before the asynchronous
  registry refresh completed. A real second-admin publication reproduced the
  missing option. Navigation now awaits fresh registry data before building the
  form. Chromium verified the new option without clicking “New” again, submitted
  a real request, and observed matching dashboard totals with no JavaScript
  errors.

The host reboot interrupted the initial full-suite regression run. The local
gateway was restarted using its existing database and keyring; no history was
removed. A post-reboot browser request again returned HTTP 200, 11 input / 6
output, and the dashboard displayed **12,381** total tokens at that point in time.
The four older usage-pending requests were not silently settled or refunded.

A further real Astra `high` request solved a small modular-arithmetic problem
and returned the correct answer `423`: 44 input, 170 output, **163 reasoning
tokens within output**, cached=0. PostgreSQL matched the response and charged
exactly **214** tokens, not 377. This freshly verifies nonzero reasoning
accounting after the reboot; the temporary key was revoked afterward.

Reproduce the browser verification explicitly (not part of the offline suite):

```bash
AUTOBUILD_LIVE_VERIFY=1 node tests/ui/codex-playground-live.mjs
```

This requires the local gateway and imported, eligible `gpt-6-astra` accounts.
It sends one real inference request and consumes real upstream quota. It creates
a temporary model without a binding, customer and short-lived gateway key, then
disables/revokes them while preserving audit records. No secrets are printed.

Earlier checkpoint regression evidence: `python -m pytest -q` completed
with **1,712 passed** in 541.23 seconds; **53 Node tests passed**. Two upstream
deprecation warnings remain (Google GenAI typing internals and Starlette's
AnyIO portal alias). Ruff, JavaScript syntax and `git diff --check` passed.
The earlier full run's two stale expectations (`ultra` metadata and rejection
of portable Chat effort) were corrected to match the already verified contract;
no product behavior was weakened to make those tests pass. These regression
results supplement, and do not replace, the real request evidence above.

## Full gateway review follow-up

The running service was restarted with its existing database/keyring after the
review repairs. No imported accounts, customer history or old pending requests
were removed. Health returned `ok` before the following fresh verification.

### Fresh real requests and weighted quota

`tests/ui/gateway-review-live.mjs` created a temporary 100-million-token customer
key with input ×1, output ×2, cache-read ×0.1 and cache-write ×1. These calls went
through the public HTTP gateway to real Codex upstream, not a fixture server:

| Request | HTTP | Input | Output | Cached | Weighted charge |
| --- | ---: | ---: | ---: | ---: | ---: |
| Responses JSON, required tool / parallel false | 200 | 125 | 17 | 0 | 159 |
| Responses SSE, same tool settings | 200 | 125 | 17 | 0 | 159 |
| Gemini, two same-name tool calls/results | 200 | 149 | 5 | 0 | 159 |
| Ollama, two same-name tool calls/results | 200 | 142 | 5 | 0 | 152 |

Both Responses envelopes retained the requested tool name, `tool_choice` and
`parallel_tool_calls`; the SSE assertion covered created and completed frames.
Gemini and Ollama both answered `12` from the two supplied tool results (3 and
9), confirming that both calls reached upstream with distinct correlation IDs.
Ollama exposes only input/output totals on its wire: its cached=0 above comes
from the saved provider usage receipt, not an invented response field.

The four completed serving attempts each matched computed and settled charge.
Total: **541 input + 44 output**, **629 weighted tokens charged**, **zero held**,
**99,999,371 remaining**. Cached and cache-write usage were zero; this run still
does not demonstrate a positive provider cache hit. All five authenticated
catalog/version GET routes were `no-store`; malformed `/api/show` returned 400.
Temporary keys were revoked and customers disabled in the scripts' cleanup.

### Real browser verification

On the real admin UI, Chromium selected an account in usage reports, visited
the second account page, and verified that the original UUID selection survived
while new page options were added. Submitting the filter used that exact UUID
and returned only matching account rows. No account edits, OAuth login or quota
refresh/reset was triggered by this read-only check.

The browser also verified a freshly published model on first Playground entry
and submitted a real `high`-effort inference: HTTP 200, `API_OK`, **11 input / 6
output**. At that point the dashboard showed **13,197 total tokens**, equal to
the backend status summary, with no JavaScript errors. This is observed data,
not a hard-coded dashboard value.

The first real WebSocket attempt established an upstream `101` handshake and
then received Codex's `codex.rate_limits` advisory before `response.created`.
Those advisories were incorrectly fed into the shared lifecycle parser and
caused a false `missing_start`/`unsupported_feature` error. The adapter now
allow-lists the three observed non-generation types (`codex.rate_limits`,
`codex.response.metadata`, and `responsesapi.websocket_timing`) and keeps all
other unknown `codex.*` frames strict. A post-fix fixture reproduced their
pre/mid-stream positions and completed with exact usage/settlement. A separate
real attempt first exposed an explicit upstream `model is not supported` error;
the adapter now classifies that pre-generation rejection and may rotate to
another eligible account. After the allow-list fix, a real WebSocket generation
completed: **12 input / 7 output**, cached=0, reasoning=0, exactly **26 weighted
tokens charged**, and **zero held** on the temporary key. HTTP Responses JSON/SSE
remained live-verified above.

Run explicitly (consumes real upstream quota):

```bash
AUTOBUILD_LIVE_VERIFY=1 node tests/ui/gateway-review-live.mjs
AUTOBUILD_LIVE_VERIFY=1 node tests/ui/codex-playground-live.mjs
AUTOBUILD_LIVE_VERIFY=1 .venv/bin/python tests/gateway/live_websocket.py
```

The browser pagination check requires at least two pages of imported accounts.
These scripts do not test a new OAuth login, KiotProxy rotation, native external
Anthropic/Gemini/Ollama provider credentials, successful Reserve inference, or
live image/compact generation. Those paths are not presented as live
successes by this review. Regression coverage of failure handling, concurrency,
protocol conversion and accounting remains separate from real-network evidence.

### Final state and regression evidence

The final full suite, started after the follow-up production fixes, completed
with **1,772 passed** in 442.46 seconds. **54 Node tests passed**; Ruff, JavaScript
syntax and `git diff --check` passed. The two existing dependency deprecation
warnings (Google GenAI and Starlette/AnyIO) remain.

The live status after the latest WebSocket success was **13,254 known tokens**,
53 completed, 4 failed and **14 pending** requests (71 total). Ten of those pending requests came from
failed WebSocket diagnostics during this review; they use temporary revoked
keys, preserve their unknown usage/holds, and are not counted as successes or
automatically refunded. The four older pending requests are also unchanged.
The gateway remains active on the original local ports with its original
database/keyring. No commit or push was performed in this review.

## Recovery and reporting continuation — 2026-09-27

Three scoped fixes followed the prior checkpoint:

- Unknown usage-pending requests without a completed usage receipt no longer
  occupy every slot of the 100-request maintenance batch. Their holds remain
  untouched; saved authoritative usage remains eligible for recovery.
- Proxy cleanup attempts the remaining leases after one release fails, then
  reports the first error. Cancellation and the existing timeout remain intact.
- Report rows and summaries use the same request-admission time window; request
  charts prefer the newly exposed `admitted_at` while retaining `started_at` for
  dispatch diagnostics and legacy chart fallback.

The report mismatch was reproduced through the running admin API before restart:
one completed request charged **17,000,000 micro-units** appeared in its summary
but not its detail rows when the window ended between admission and dispatch.
After restart, **106 real report windows** (53 completed requests, each checked
through request and account reports) had matching summary/detail charges and
included the previously omitted row. These were read-only history checks.
One initial post-restart check mistakenly requested `limit=500` instead of the
allowed maximum 200; its "no data" result was invalid and discarded. The corrected
check explicitly checked response status and verified all 106 windows.

A fresh real Codex WebSocket inference then completed: **12 input / 7 output**,
cached=0, reasoning=0, **26,000,000 micro-units charged** (input ×1, output ×2),
and **zero held** on its temporary key. The script revoked the key and disabled
its temporary customer after checking the saved request and balance.

Fresh full regression: **1,779 Python tests passed** in 558.88 seconds;
**55 Node tests passed**. Ruff, compileall, changed JavaScript syntax and
`git diff --check` passed. The two existing dependency warnings remain.
A scoped independent review found no Critical or Important issue in these fixes.
The earlier suite run was intentionally interrupted after 294 passes when the
chart follow-through was added; it is not the final verification result.

Final live status: **72 requests / 54 completed / 4 failed / 14 pending**;
12,623 input + 650 output = **13,273 known tokens**, including 265 reasoning
tokens within output. All 14 previous pending holds remain unchanged.
Service is active and `/health` returns `{"status":"ok"}`. Database/keyring and
existing unrelated changes were preserved; no commit or push was performed.

### Continued frontend/backend review — 2026-09-27

After the preceding checkpoint was pushed as `ababff8`, an independent review
found and reproduced three runtime issues: the bulk refresh worker used a
Python 3.11-only API despite Python 3.10 support; public WebSocket handshakes
did not consume the HTTP pre-authentication IP limit; and admin fetches lost
status when an upstream/reverse proxy returned HTML instead of JSON. Regression
tests were written failing first, then fixed. A fresh Python 3.10 environment
initially passed 36 targeted tests; the clean Python 3.14 environment initially
exposed the separate missing-`greenlet` packaging issue, now fixed with
`sqlalchemy[asyncio]`. Full clean-environment suites then passed **1,784 Python
tests on Python 3.10 and 3.14**; the Node suite passed **57 tests**, with Ruff,
compileall, JavaScript syntax, diff checks and a clean wheel build also passing.
A scoped second review found no Critical/Important issue.

Read-only real browser verification after those fixes returned `API_OK` from
Playground (11 input / 6 output), retained the selected account across report
pagination, showed 13,290 total dashboard tokens, and reported zero JavaScript
errors. No OAuth login, account edit, quota refresh/reset, or proxy mutation was
performed by that check.
