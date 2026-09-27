# Bounded admission and proxy capacity for 100 concurrent users

Date: 2026-09-27

## Intent

The gateway must accept a burst from at least 100 distinct users without
immediate capacity-related 503 responses. Requests must wait for the real
provider and proxy capacity with a bounded deadline, then complete through the
configured proxy pool. The gateway must continue to account input, cached,
output, and reasoning tokens exactly once and must not silently fall back to a
direct connection.

The current production evidence is a 100-request burst with 16 successful
requests and 84 `upstream_unavailable` responses. The provider admission limit
is 16 and each configured proxy endpoint is leased exclusively by one request.
The 503s are therefore local admission failures, not proof that the HTTP proxy
pool is offline.

## Goals

* Admit up to 100 in-flight inference requests per gateway process, including
  requests waiting for provider or proxy capacity.
* Preserve FIFO-ish fairness, cancellation, request deadlines, and bounded
  memory. A disconnected or expired request must leave the queue and release
  any quota hold/resources it owns.
* Keep provider, credential, quota, and proxy limits authoritative. The queue
  must not raise an upstream limit or bypass a provider 429/cooldown.
* Keep the existing proxy policy: a configured proxy is required whenever the
  selected route requires one; direct fallback is never implicit.
* Make queue depth and wait time observable for the admin/status surface and
  load-test artifacts.
* Support the current 20 HTTP proxies and KiotProxy entries without holding a
  database connection while a request waits.

## Non-goals

* This change does not promise 100 simultaneous upstream generations. Actual
  generation concurrency remains bounded by provider/credential limits and
  proxy endpoint capacity.
* It does not change OAuth, account rotation, quota reset semantics, model
  capabilities, or token prices.
* It does not add direct-proxy fallback, automatic proxy replacement, or
  provider-limit inflation.
* It is initially process-local. A later multi-worker deployment can assign a
  per-worker budget or replace the gate with a shared database semaphore; this
  change must not pretend an in-memory counter is globally shared.

## Design

### 1. Bounded in-flight gate

Add a small asynchronous admission gate owned by the gateway `Engine`. The
gate has a configurable `max_inflight` (default 100, minimum 1) and a FIFO
waiter deque. `enter(deadline)` either receives a permit or waits until the
request deadline. The gate counts both running and waiting requests, so a
burst of exactly 100 distinct users is admitted; request 101 receives a
stable, documented 429/503 queue-full error instead of unbounded memory use.

The gate is entered after request authentication/policy validation and quota
reservation, but before provider admission or proxy acquisition. This keeps
invalid requests cheap while ensuring that a valid request has a bounded place
in the resource queue. The permit is held until the generation/stream closes
and is released from an `AsyncExitStack` on success, cancellation, disconnect,
or any preparation failure.

The gate does not hold a SQLAlchemy session, proxy lease, provider admission,
or account lock while waiting. Queue wait uses an asyncio condition/event and
short, cancellation-safe wakeups. A deadline or disconnect removes the waiter
exactly once.

### 2. Capacity-aware provider admission

Keep `ProviderLimits` as the source of truth for provider and credential RPM,
cooldown, and active-concurrency limits. Add a wait mode used by inference
dispatch: capacity exhaustion waits for a release/cooldown until the request
deadline instead of immediately raising `upstream_unavailable`.

The database transaction remains short: check/insert an admission row, commit,
release the connection, and notify local waiters on release. No polling loop
keeps a transaction open. Provider 429, disabled provider/credential, and
other permanent errors remain immediate errors. The existing non-waiting mode
used by catalog/probe paths remains unchanged.

### 3. Proxy capacity and retry classification

Keep one-owner leases for static HTTP endpoints and Kiot key/endpoint pairs by
default. Extend proxy acquisition to distinguish local capacity exhaustion
from an invalid/disabled proxy or Kiot failure. `route_resources` retries only
the capacity-exhausted case while the same request deadline remains valid;
permanent proxy errors still surface immediately.

Provider and proxy resources are acquired in the existing safe order (provider
admission first, proxy lease second), and both are released before the gate
permit is released. If proxy capacity disappears after provider admission, the
provider slot is released before a retry so queued requests cannot pin all
provider slots while waiting for a proxy. The retry backoff is bounded and
cancellation-safe.

The configured proxy endpoint concurrency remains 1 for this phase. This is
deliberate: it avoids overloading the 20 LAN proxies and preserves KiotProxy
key exclusivity. Endpoint sharing can be added later as an explicit, measured
setting rather than being inferred from the 100-user requirement.

### 4. Configuration and status

Add a gateway setting for the gate capacity (environment/config default 100)
and a maximum queue wait derived from the existing request deadline. Expose
read-only counters in the service status/health payload:

* `inflight`, `queued`, `capacity`;
* accepted, queue-full, expired/cancelled counts;
* queue wait p50/p95 and the last observed wait;
* provider/proxy capacity waits and capacity-retry counts.

No secret, email, OAuth token, proxy URL credential, or raw upstream error is
included in these counters. Existing health requests remain independent of the
inference gate so operators can diagnose a saturated queue.

### 5. Error and accounting rules

* Queue full is a safe `gateway_busy` 429 response with `Retry-After`; it is
  not recorded as an upstream generation.
* Queue expiry/disconnect releases the unspent ledger hold as
  `not_dispatched`; it never charges fabricated usage.
* Once `mark_dispatched` succeeds, failures retain the existing pending/usage
  reconciliation behavior. No retry is introduced for an ambiguous stream or
  network failure.
* A provider 429/cooldown is returned according to the existing retry policy;
  waiting for local capacity must not hide an upstream rate limit.
* Proxy leases and provider admission rows are fenced and released exactly
  once, including cancellation during queue wait, provider wait, proxy wait,
  stream close, and client disconnect.

## Data flow

1. HTTP boundary validates the request and authenticates the API key.
2. Routing/policy selects a route and the ledger reserves the worst-case hold.
3. The request enters the bounded gate and records queue wait metrics.
4. The route waits for provider/credential capacity without holding a DB
   connection, then obtains the configured proxy lease.
5. Egress validation and the existing pre-dispatch revalidation run.
6. The adapter sends the real request through the selected proxy and streams the
   response; usage is settled from the actual provider response.
7. Exit-stack cleanup releases stream, proxy, provider, gate permit, and any
   unspent hold in that order.

## Verification plan

Unit coverage will cover FIFO ordering, queue-full behavior, deadline expiry,
cancellation, double-release protection, provider wait/release notification,
proxy capacity retry classification, and cleanup on every pre-dispatch path.

Integration coverage will use PostgreSQL and the real lease/admission stores to
prove that no connection is held while waiting, no active admission/lease rows
leak, and quota rows settle correctly after queue expiry or cancellation.

The final validation is a real 100-user burst against the running gateway with
the configured 20 HTTP proxies (no fake upstream responses):

* 100 requests are accepted into the bounded gate;
* zero capacity-related 503 responses occur while the queue has capacity;
* all completed responses contain real usage, with input/cached/output/
  reasoning totals matching persisted ledger data;
* no more than the configured provider/credential and proxy limits are active;
* health/admin requests remain responsive;
* queue p95 and end-to-end p95 are reported, along with any genuine upstream
  429/5xx or proxy failure.

If the 180-second request deadline prevents all 100 from completing under the
measured upstream latency, the result will be reported as a capacity/latency
limit rather than mislabeled as a successful 100-user guarantee. The next
change would then be an explicitly approved capacity increase or endpoint
concurrency setting.

## Rollback and rollout

The gate is disabled only by setting its capacity to the previous effective
behavior and restarting the gateway; existing provider/proxy settings remain
untouched. Rollout starts with `max_inflight=100`, one gateway worker, and the
current proxy pool. Increase provider or endpoint concurrency only after the
real burst demonstrates that the upstream and proxies sustain it.
