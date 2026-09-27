# Gateway reference study — 2026-09-27

This is a read-only study of the requested repositories. The checkouts are
under `.deps/gateway-research/`, are ignored by the product build, and are not
vendored into this project. Findings about those repositories are source
inspection only; they are not live availability or performance claims.

## Checkouts and licensing

| Repository | Commit inspected | License/source note |
|---|---|---|
| Portkey-AI/gateway | `669825cbe89ee51569918b8f78a9db486fd69dd4` | MIT |
| QuantumNous/new-api | `c2b7a9a9e0b548c2051a949fceabb59029adcb49` | AGPL-3.0; do not copy into this codebase without a separate licensing decision |
| Azure-Samples/AI-Gateway | `1679e3137d2fe7592d2a668d77ff71550d35b69e` | MIT; APIM/Bicep/lab material, not a local FastAPI gateway |
| pokon548/ai-gateway-openai-wrapper | `df0d3ac29cfdd6da004e1e6abe44a31a0c5d1bcb` | AGPL-3.0; Cloudflare Worker wrapper |
| GautamVhavle/CatGPT-Gateway | `a4eb697a31e52274af25fd668bcf6973d1d68122` | MIT; browser automation and account-session risk |
| AuuCoder/gptGrok2api | `4adce806b3a1f0ef167e21385c2b78399e07af7c` | MIT; bundled solver/source licenses require separate review |
| GetGoAPI/Free-GPT-API-Proxy-Gateway | `bf0549bf02de858831f78d3d9c8da41df7d87bf5` | No source license found; only static files and a prebuilt JAR |
| tmq9999/hyperagent-unified-gateway | `82707867e0ab19d619de043360bcd82b7da5dc9a` | MIT |
| tmq9999/postman-gateway-unoffical | `0bf6887bab32be3c2e5f28c82efcc10f4b652b36` | `pyproject.toml` declares MIT; no standalone LICENSE found |
| tmq9999/Gitlab-Gateway | `e0eb2633dc8d349a4a53b251b8658416679e84b6` | README claims MIT; no LICENSE file found in the checkout |

The existing Cockpit reference is separately pinned at
`.deps/cockpit-reference`, commit `ee13174927f04f76d975f59d20960fc4cf800ea9`.
It is used as behavioral reference only, not copied as a sidecar or source
dependency.

## What is worth adopting

### Capacity, admission, and fairness

Cockpit's `sidecars/cockpit-cliproxy/account_concurrency.go` is the closest
match to the immediate problem. It has an explicit maximum waiting count of
100, a release notification with a polling fallback, account-slot selection
that tries other accounts before waiting, and an outer gate that also covers
session-affinity hits. Its `account_concurrency_test.go` covers preferred
account saturation, all-candidate saturation, timeout, disabled waiting, and
release cleanup.

Portkey contributes bounded retry policy: retry only configured statuses,
respect `Retry-After`, and stop when the remaining request budget cannot hold
another attempt (`gateway/src/handlers/retryHandler.ts`). Its Redis Lua token
bucket is a good design for a future multi-worker ingress limiter, but the
current gateway must not pretend a process-local counter is globally shared.

CatGPT-Gateway demonstrates semaphore-bounded browser/page resources and
per-session locks with LRU eviction. gptGrok2api provides useful operational
defaults to study: separate text/image capacities, a queue timeout, account
health, in-flight request de-duplication, and bounded response caching. These
are patterns to adapt, not defaults to copy blindly.

Azure APIM demonstrates the control-plane separation we need: token limits per
subscription, backend load balancing, retry on 429/503, semantic cache policy,
managed identity, and telemetry. The APIM/Bicep implementation itself is not
portable to our FastAPI service.

### Protocol and upstream isolation

new-api has the broadest protocol conversion reference: OpenAI Chat/Responses,
Anthropic Messages, Gemini, images/audio, embeddings, and Realtime/WebSocket,
with conversion fixtures under `relaykit/relayconvert/`. We should reproduce
the contracts and add our own tests rather than copy AGPL code.

Hyperagent and Gitlab-Gateway provide two useful local patterns: an explicit
client-model to upstream-model map, durable response/session continuity, and a
single-flight token exchange per account with proactive expiry and 401
invalidation. Gitlab also strips credential-like headers before forwarding
cached headers.

Postman-Gateway shows a small, clear three-surface protocol boundary (Chat,
Anthropic, Responses), session-to-conversation mapping, bounded query input,
and explicit connect/read timeouts. Its caller authentication is a placeholder
and its image/file paths are incomplete, so those parts are not a model.

The Cloudflare wrapper is useful only for one security idea: keep the public
dummy key separate from the real upstream key. Its permissive CORS, weak token
parsing, unrestricted body/header forwarding, and missing rate limits are
explicit anti-patterns for our service.

Free-GPT-API-Proxy-Gateway cannot provide implementation evidence: the checkout
contains static assets and a prebuilt Spring JAR but no request-routing source
or clear license. Its advertised third-party endpoint is not used.

## Live evidence from our running gateway

These observations were made against the actual local service, not a mock:

* `autobuild-local-gateway` is active; public listener is `127.0.0.1:8788`.
* `GET /health` returned HTTP 200.
* The configured provider returned a real `POST /v1/responses` response with
  HTTP 200 and usage: input 12, cached 0, output 5, reasoning 0. The returned
  output was non-empty and the temporary API key/customer were revoked/deleted
  afterward.
* Admin status reported 249 accounts (196 active, 5 refresh-uncertain, 48
  disabled) and one enabled provider with RPM 600 and active concurrency 16.
* All 20 configured HTTP proxies (`192.168.1.17:7001`–`:7020`) returned the
  real Google `generate_204` probe with HTTP 204 in approximately 320–543 ms.
* The latest real 100-request burst produced 16 successful responses and 84
  local `upstream_unavailable`/503 responses. Health stayed responsive, but
  this is not yet a 100-user guarantee. The measured cause is the provider
  concurrency cap of 16 plus exclusive one-request proxy leases and no bounded
  inference queue.

The reference repositories were not called live because no safe, authorized
deployment and upstream credentials were available for them. No result in this
document treats a source test, a mocked response, or a README claim as live
gateway evidence.

## Recommended development direction

1. **Bounded admission gate (first change).** Add a process-local FIFO-ish
   gate with capacity 100 and a documented queue-full 429. Count waiting and
   running requests, remove cancelled/expired waiters exactly once, and never
   hold a database session, provider admission, proxy lease, or account lock
   while waiting.
2. **Wait-not-reject provider admission.** Keep provider/credential RPM,
   cooldown, and active limits authoritative. For inference only, wait for a
   release until the request deadline; preserve immediate errors for disabled
   credentials, upstream 429/cooldown, and permanent failures. Release the
   provider slot before retrying local proxy capacity.
3. **Preserve proxy safety.** Keep static endpoints and Kiot key/endpoint pairs
   single-owner for the first rollout. Distinguish capacity exhaustion from an
   invalid proxy; never silently fall back direct. Add shared endpoint
   concurrency only as a separately measured, explicitly approved setting.
4. **Make capacity observable.** Expose queue depth, inflight, accepted,
   queue-full, expiry/cancel, wait p50/p95, provider-capacity waits, and
   proxy-capacity retries without exposing secrets or prompts. Keep `/health`
   independent of the inference gate.
5. **Strengthen protocol boundaries.** Preserve existing OpenAI Responses,
   Chat Completions, compact, images, and WebSocket behavior. Add a normalized
   internal usage record and verify that input/cache/output/reasoning usage is
   settled exactly once for streamed and non-streamed responses.
6. **Add account/session safeguards.** Use per-account single-flight refresh,
   proactive expiry, 401 invalidation, and session-affinity-aware capacity
   selection. Do not mark quota recovered from an unconfirmed response.
7. **Live verification gates.** After each implementation slice, run a real
   request through the configured proxies and capture status, non-empty output,
   usage, persisted ledger rows, active admission/lease counts, health latency,
   and cleanup. The acceptance run is 100 real distinct API keys with no fake
   upstream and reports queue p95/end-to-end p95 plus genuine upstream errors.

This sequence targets Cockpit's proven scheduling behavior while retaining our
own FastAPI/PostgreSQL implementation, exact token accounting, API-key quota,
and proxy policy.

## Open design decision

The existing design spec proposes “100 users accepted into a bounded queue,”
not “100 simultaneous upstream generations.” The live provider is currently
limited to 16 active requests and each proxy endpoint to one leased request.
Before implementation, approve that distinction and the first rollout values:
`max_inflight=100`, one gateway worker, provider limit unchanged at 16, and
static/Kiot endpoint concurrency unchanged at one.
