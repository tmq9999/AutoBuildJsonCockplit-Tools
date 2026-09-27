# Bounded admission settlement: final live acceptance

Date: 2026-09-28 (Asia/Bangkok)
Reviewed behavior revision: `befb95349b908f02002a87e83de905db9c2f0619`
Runtime: `autobuild-local-gateway`, MainPID `293688`, worktree
`bounded-admission-capacity`; service restarted at 02:14:21 +07.

## Scope and controls

The run used capacity 100, provider concurrency 16, one-owner proxy leases,
and the configured 20 HTTP proxies. Preflight
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

| Phase | Sent | Completed | HTTP/error observations | Pending after phase | Ledger mismatches | Cleanup |
|---|---:|---:|---|---:|---:|---|
| Single non-stream + stream | 2 | 2 | 0 | 145 | 0 | 1 key revoked; customer disabled; verified |
| 100-key burst | 100 | 89 | 9 HTTP 429 (`rate_limited:upstream`), 2 HTTP 502 (`upstream_error:request`; origin unestablished) | 147 | 0 | 100 keys revoked; customer disabled; verified |
| 100 users × 3 dependent steps | 263 | 240 | 22 HTTP 429 (`rate_limited:upstream`), 1 HTTP 502 (`proxy_error:upstream`; root cause unestablished) | 148 | 0 | 100 keys revoked; customer disabled; verified |

Single requests were the escalation gate: both had HTTP 200, terminal output,
authoritative input/output usage, exactly one settlement each, and no cleanup
errors. Burst/workflow results therefore remain valid evidence but are not a
claim of upstream stability: HTTP 429/502 outcomes occurred, with the exact
gateway `error_code:error_stage` values shown above. A request-stage 502 does
not establish an upstream or proxy root cause.

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
its keyed request row on the observed usage fields. The database-stored usage
arithmetic then matched the single settlement row per request, with zero
reconciliation mismatches in all phases. This is not an independent full
five-field response-to-money proof: the public receipts did not expose
`cached_write`, so the harness response-charge branch requiring every field
was not exercised. Duplicate attempt rows were reported as diagnostics and
did not mask settlement-row counts.

The public responses did not expose cache-write usage in these phases.
`cached_write` is therefore reported as unknown/unobserved, never defaulted as
proof of zero; this absence does not prove whether the upstream value was zero
or whether a serializer omitted it. Cached-read was observed as zero where
present. Unknown usage requests remain `usage_pending` and were not charged or
released.

## Wait and latency aggregates

Single p50/p95 latency was about 3.2 s; burst p50/p95 was 27.8/43.1 s; workflow
p50/p95 was 25.5/63.8 s. Workflow provider wait p95 was about 42.7 s and
resource wait p95 about 34.1 s; no provider/proxy/upstream deadline expiries
were recorded. Health probe maxima were about 2.3 s during load. Capacity
telemetry reported zero `rate_limited_by_stage` deltas despite 9 and 22 HTTP
429 responses in the burst and workflow. This is a telemetry/provenance gap:
the stage deltas do not establish the source of those HTTP 429 responses and
are not evidence that throttling did not occur.

The live evidence supports bounded admission, observed-usage matching, and
exactly-once settlement for completed requests. It does not establish 100%
upstream success or stability;
the recorded HTTP 429/502 outcomes, their unproven origin, and pending unknown
usage remain the operational result.
