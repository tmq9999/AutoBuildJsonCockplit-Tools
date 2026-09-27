# Task 4 private operational report

Reviewed and pushed behavior revision: `befb95349b908f02002a87e83de905db9c2f0619`.
Public live report commit: `f222714` (`docs: record settlement repair acceptance`).
Evidence-qualification correction commit: `19e3b3f` (`docs: qualify live settlement evidence`).
Neither commit was pushed by this worker. No PR or historical reconciliation was performed.

## Commands and exits observed in this worker

- `git rev-parse HEAD`: exit 0, matched `befb95349b908f02002a87e83de905db9c2f0619` before restart.
- `systemctl --user restart autobuild-local-gateway`: exit 0. MainPID changed `188511` to `293688`; active/running and worktree cwd observed. Public `/health`: HTTP 200, 22.159 ms. Admin `/api/service/codex-service/status`: HTTP 200, configured capacity 100, settlement 145 pending / 9 recoverable.
- `AUTOBUILD_GATEWAY_PID=293688 PYTHONPATH=. python3 data/bounded_acceptance_verify.py preflight`: exit 0; no preflight errors; exact configured 20 HTTP proxies, provider limit 16, capacity 100, runtime PID/cwd confirmed.
- Same command with `single`: exit 0; `burst`: exit 0; `workflow`: exit 0. These are real upstream calls with no client retries.
- Harness prep: `ruff check data/bounded_acceptance_verify.py` exit 0, `python3 -m py_compile data/bounded_acceptance_verify.py` exit 0, parser fixture check exit 0. Controller supplied full gateway suite result 1,738 passed and focused/Ruff/compile/diff clean for reviewed revision; this worker did not rerun that suite.
- Public report commit `f222714`, later correction `19e3b3f`; `git diff --check HEAD^ HEAD` exit 0 after each commit.

## Protected evidence locations

All paths below are under the worktree's `data` symlink to the original checkout's persistent data. Each run directory is mode 700 and individual evidence files are mode 600. Do not publish or print raw response bodies. The operational harness itself is ignored/uncommitted.

| Phase | Directory | Aggregate source | Other relevant evidence |
|---|---|---|---|
| Post-restart preflight | `data/.bounded-acceptance/20260927T191442Z-905e122e-3283-4c0d-8147-477956826ca0` | `summary.json` | `preflight.json` |
| Single | `data/.bounded-acceptance/20260927T191456Z-cc9e16dd-c711-447d-9a0b-85bd6f1d015f` | `summary.json` | `records.json`, `request-reconciliation.json`, `preflight.json`, two `response-*.json` files |
| Burst | `data/.bounded-acceptance/20260927T191554Z-7a905b90-ddb8-484e-8ffa-f0d6c3a26095` | `summary.json` | `records.json`, `request-reconciliation.json`, `preflight.json`, `response-*.json` |
| Three-step workflow | `data/.bounded-acceptance/20260927T191714Z-8b803881-e3f5-416c-af35-a98887ff2182` | `summary.json` | `records.json`, `request-reconciliation.json`, `preflight.json`, `response-*.json` |

## Acceptance evidence and limits

Single gate passed: nonstream and stream were HTTP 200 terminal responses with nonempty text and authoritative input/output usage; SQL found two completed requests, two settlement rows, zero mismatches. The one temporary key was revoked and its customer disabled; SQL cleanup verification was true. No new pending usage, live provider admission, or run-owned proxy lease remained. This gate authorized the burst/workflow phases.

Burst: 100 requests, 89 completed, 9 HTTP 429 `rate_limited:upstream`, 2 HTTP 502 `upstream_error:request`, 2 run pending, zero recorded reconciliation mismatches. Workflow: 263 requests across 100 users' dependent three-step prompts, 240 completed, 22 HTTP 429 `rate_limited:upstream`, 1 HTTP 502 `proxy_error:upstream`, 1 run pending, zero recorded reconciliation mismatches. In both phases all 100 temporary keys were revoked and customers disabled, with SQL verification true. Run-owned proxy leases and live provider admissions returned zero; 2 other proxy owners and 6 expired provider rows were preserved. Pending global count progressed 145 → 147 → 148; 9 historically recoverable receipts remained untouched.

Observed public receipt fields matched persisted request usage and each completed request had one settlement row. Stored usage arithmetic matched the ledger charge. Public `cached_write` was absent in every completed receipt; the harness's full response-to-charge comparison branch requires all five fields and did not run. Thus zero reconciliation mismatches do not prove independent full-field monetary equality. Absence in the public response does not establish upstream omission or zero value. Raw response evidence remains private.

The two burst 502s were `upstream_error:request`, so origin is unestablished. `rate_limited_by_stage_delta` was zero in burst and workflow despite 9 and 22 observed HTTP 429s; the counter does not explain their origin. The available saved attempt rows cannot identify the exact count of gateway pre-response proxy retries. These are evidence/provenance limits, not assertions that no throttling or retries occurred.
