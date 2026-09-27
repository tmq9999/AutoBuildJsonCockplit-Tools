# Bounded admission settlement: final live acceptance

Date: 2026-09-28 (Asia/Bangkok)
Reviewed behavior revision: `befb95349b908f02002a87e83de905db9c2f0619`
Runtime: `autobuild-local-gateway`, MainPID `293688`, worktree
`bounded-admission-capacity`; service restarted at 02:14:21 +07.

## Scope and controls

The run used capacity 100, provider concurrency 16, one-owner proxy leases,
and the configured 20 HTTP proxies (`192.168.1.17:7001..7020`). Preflight
confirmed the exact proxy pool, provider limit, live serving capacity, and
healthy public/admin listeners. Client retries were disabled. Raw response
evidence is retained only under mode-700 run directories with mode-600 files;
stdout and this report contain redacted aggregates only.

## Runtime checks

- Public `/health`: HTTP 200; observed health probes were all 200.
- Admin `/api/service/codex-service/status`: HTTP 200, configured capacity
  100. Initial settlement snapshot: 145 `usage_pending`, 9 recoverable with
  authoritative attempt usage, oldest pending age about 37.3 hours.
- Historical rows were preserved: 202 reserved, 6 expired provider rows, and
  2 background proxy owners before the live phases. No historical reconciliation
  or broad maintenance was run.

## Acceptance outcomes

| Phase | Sent | Completed | Upstream errors | Pending after phase | Ledger mismatches | Cleanup |
|---|---:|---:|---|---:|---:|---|
| Single non-stream + stream | 2 | 2 | 0 | 145 | 0 | 1 key revoked; customer disabled; verified |
| 100-key burst | 100 | 89 | 9 rate-limited, 2 upstream 502 | 147 | 0 | 100 keys revoked; customer disabled; verified |
| 100 users × 3 dependent steps | 263 | 240 | 22 rate-limited, 1 upstream proxy error/502 | 148 | 0 | 100 keys revoked; customer disabled; verified |

Single requests were the escalation gate: both had HTTP 200, terminal output,
authoritative input/output usage, exactly one settlement each, and no cleanup
errors. Burst/workflow results therefore remain valid evidence but are not a
claim of upstream stability: upstream rate limits and errors occurred.

Observed maxima were provider 16 and proxy leases 20. After each phase, live
provider admissions and run-owned proxy leases were zero; two unrelated
background proxy owners remained and were preserved. Queue gauge returned to
zero. No client retries were issued. The harness cannot classify internal
gateway pre-response proxy retries from the saved attempt rows and reports that
limitation explicitly.

## Usage and settlement evidence

Single usage totals were input 34, output 62, reasoning 36. Burst totals were
input 1,513, output 1,788, reasoning 623 across 89 authoritative responses.
Workflow totals were input 11,121, output 16,663, reasoning 8,064 across 240
authoritative responses. Each authoritative terminal response was matched to
its keyed request row; weighted charge matched the single settlement row per
request, with zero mismatches in all phases. Duplicate attempt rows were
reported as attempt diagnostics and did not mask settlement-row counts.

The upstream responses did not provide cache-write usage in these phases.
`cached_write` is therefore reported as unknown/unobserved, never defaulted as
proof of zero. Cached-read was observed as zero where present. Unknown usage
requests remain `usage_pending` and were not charged or released.

## Wait and latency aggregates

Single p50/p95 latency was about 3.2 s; burst p50/p95 was 27.8/43.1 s; workflow
p50/p95 was 25.5/63.8 s. Workflow provider wait p95 was about 42.7 s and
resource wait p95 about 34.1 s; no provider/proxy/upstream deadline expiries
were recorded. Health probe maxima were about 2.3 s during load.

The live evidence supports bounded admission and exact settlement for
authoritative usage. It does not establish 100% upstream success or stability;
the recorded upstream 429/502 outcomes and pending unknown usage remain the
operational result.
