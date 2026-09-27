# Bounded admission live acceptance — final revision

Date: 2026-09-27  
Revision: `e316195b30c54d9999835e0bf43c489447ae5924`  
Runtime: serving worktree `feature/bounded-admission-capacity`, admission capacity 100, provider limit 16, 20 configured HTTP proxies (`192.168.1.17:7001`–`7020`).

## Verification gates

- Full `tests/gateway`: **1701 passed**, one dependency deprecation warning.
- Ruff, compileall, and `git diff --check`: passed.
- `/health` after controlled restart: HTTP 200, `{"status":"ok"}`.
- Preflight: exact proxy pool/provider/schema checks passed; no preflight errors.
- Persistent `data` symlink was preserved and still targets the original checkout data directory.

## Real upstream evidence

### Single non-stream + stream

- 2 requests sent; 2 admitted and completed with HTTP 200.
- 2/2 had non-empty output and usage; streaming reached `response.completed`.
- Ledger: 2 entries, 0 mismatches; usage-pending: 0.
- Provider active maximum: 1; proxy lease maximum: 1.
- Health probes: all HTTP 200, maximum 9.07 ms.
- Temporary key revoked and customer disabled; provider/proxy live rows after cleanup: 0.

### 100-user burst (no client retry)

- 100 requests admitted/recorded; 93 completed with HTTP 200 and real usage.
- 7 requests returned genuine `rate_limited:upstream` (not gateway capacity errors).
- Usage present: 93/93; ledger: 100 entries, 0 mismatches; usage-pending: 0.
- Provider and proxy maxima: 16 each; health probes all HTTP 200, maximum 3.76 s.
- Queue gauge after cleanup: 0; no gateway-busy/capacity error was observed.
- All 100 temporary keys revoked and customer disabled; proxy live rows: 0.

This demonstrates admission of a 100-request burst while preserving the configured provider/proxy limits. It does **not** claim 100 successful generations: upstream rate limiting rejected 7 requests.

### Secondary 3-step workflow

- 289 real requests were sent; 278 completed with usage.
- Genuine outcomes: 8 `rate_limited:upstream`, 2 `proxy_error:upstream`, 1 `deadline_exceeded:upstream`.
- At workflow end: 3 requests remained `usage_pending` with reason `interrupted`; this is retained as an unresolved reconciliation state, not reported as zero-leak success.
- Ledger: 286 entries, 0 mismatches; provider/proxy maxima: 16 each; health probes all HTTP 200, maximum 3.18 s.
- All 100 temporary keys revoked and customer disabled; proxy live rows: 0.

The workflow result is a secondary latency/error observation. The upstream/proxy failures and pending interrupted settlements prevent an unconditional “stable 100-user” claim for multi-step workloads.

## Residual database state

The preflight and post-run snapshots consistently showed six expired provider-admission rows from earlier runs. They were not modified or deleted. No live provider owners or proxy owners from the measured runs remained after cleanup.

## Decision

The bounded gateway revision is deployed and verified with real responses. The 100-user admission objective is met at the gateway boundary, with provider concurrency capped at 16 and proxy ownership capped by the configured pool. Upstream rate limits, two proxy errors, one deadline expiry, and three interrupted usage settlements remain measured limitations; increasing concurrency or silently clearing those rows is not justified by this evidence.
