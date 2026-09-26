# Gateway compatibility — development evidence

This matrix describes tested behavior, not full vendor API parity or production
readiness. Provider I/O is synthetic in these tests; SDK traffic uses real HTTP to
an isolated Uvicorn listener and a real temporary PostgreSQL 17.6 server.

| Client SDK pinned in tests | JSON text | Streaming text | Endpoint tested |
|---|---|---|---|
| openai 3.19.1 | pass | pass | `/v1/chat/completions`, `/v1/responses` |
| anthropic 1.8.0 | pass | pass | `/v1/messages` |
| google-genai 2.25.0 | pass | pass | Gemini generateContent / SSE streamGenerateContent |
| ollama 0.6.2 | pass | pass | `/api/chat`, `/api/generate` NDJSON |

Wire-level tests additionally cover native OpenAI Responses, Anthropic, Gemini,
Ollama and Codex responses, tool call/result mappings, model permissions and usage
normalization. They do not prove every SDK tool mode, beta feature, model or client
application (Claude Code/Codex CLI) works. Run the relevant client acceptance test
before claiming that support.

## Current limits

- Responses WebSocket is a sequential beta bridge, not a transparent persistent
  upstream tunnel. Wait for a terminal event before the next `response.create`.
  It authenticates every turn, echoes valid `stream_id` on events/errors and uses
  configured guarded egress. Multiplexed lanes, `generate:false` warmup and native
  socket-cache continuity are not implemented. A new upstream socket per turn
  means an encrypted local continuation handle alone cannot ensure the upstream
  still has the prior response; provide full context where necessary. The local
  connection limit is 30 minutes, idle 5 minutes, inbound message 16 MiB.
  Validation of stream IDs and error correlation was checked against the
  [official Responses WebSocket guide](https://developers.openai.com/api/docs/guides/websocket-mode).
- Text and basic function/tool calls are the implemented core. Native Responses
  reasoning summaries and opaque encrypted replay are preserved only on Responses;
  raw chain-of-thought is never exposed or translated to another protocol. Vision,
  provider-specific tool features and unsupported combinations fail closed with a
  regression test rather than being silently translated.
- Responses background/store=true/retrieval remain unsupported. Codex OAuth compact
  uses `/v1/responses/compact`; image generation/edit and Responses WebSocket beta
  are available only when the selected Codex binding advertises the capability.
  Continuation handles are tenant/key/model/route/credential scoped, encrypted and
  enabled only for a native route with that capability. Chat-backed Responses
  requires complete history in each request.
- Native Anthropic/Gemini count endpoints are available; translated routes lacking
  a trustworthy counter return unsupported. Counts share the key's RPM/concurrency
  gate and do not charge generation quota. Hard money-budget counting routes require
  a separate counter cost policy and currently fail closed.
- Codex OAuth strips `max_output_tokens`, `temperature`, and `top_p` before its
  upstream Responses call, matching Cockpit's compatibility normalization. These
  are accepted client hints, not enforced generation caps; admission and retry
  reservation still use the full finite operator-configured model bound. Codex
  requests add `Accept: text/event-stream`; no `originator` spoofing is used. No
  live Codex inference contract has been verified by this gateway implementation.
- Model discovery via public APIs is filtered by client policy. Private provider
  discovery supports native model-list shapes and stages entries for admin review.
- Public gateway requires a client key for every protocol, including Ollama. No key
  query parameter; Gemini `alt=sse` is an allowed nonsecret query.
- The approved local admin UI is implemented at `/service/`. Existing OAuth workbench
  now also offers explicit direct/fixed/pool/Kiot selections; old default payloads work.

## Verification snapshot

2026-09-26 transport checkpoint: **1,612 Python tests passed** on Python 3.14
with disposable PostgreSQL 18, **43 Node tests**, Codex Chrome smoke, Ruff,
compileall and diff checks passed. One browser fixture socket hangup occurred;
the rerun passed. WebSocket tests exercise ASGI auth/boundaries, two turns,
cached-token accounting, policy changes, cancellation/unknown usage, malformed
handshakes and real httpcore HTTP upgrade/CONNECT with synthetic network I/O.
They make no real Codex, KiotProxy or third-party inference call.

Rate-limit final local checkpoint: **549 Python tests on both 3.10 and 3.14**, 12
Node tests, both Chrome E2E suites, Ruff/compileall/package build passed. Independent
review and focused re-review found no remaining Critical/Important issues. One
Minor error-ordering edge remains: an old continuation whose binding is reassigned
to another credential may receive 429 while that replacement credential is cooling,
rather than invalid_state. It is not dispatched on the replacement credential.

Rate-limit follow-up: generation adapters and native Anthropic/Gemini counters
preserve HTTP 429 and a bounded numeric/HTTP-date `Retry-After`. JSON error bodies
follow the inbound protocol, including when a streaming request is rejected before
opening its stream. Unknown/mixed/partially attempted fallback chains do not claim
a retry time. A subsequent request blocked entirely by stored cooldowns receives
the earliest local eligibility time instead. This is scheduling guidance, not a
guarantee of upstream capacity. Automatic generation replay remains limited to
one alternate provider after a definite 429 before generation; uncertain failures
and started streams are not replayed. Structured/prose retry hints in provider
response bodies are not parsed.

Reference behavior was examined in FreeLLMAPI commit
`1346b7d39ecbc71ac4c087fcdd36248ebdbde79b` (cooldown architecture and chain retry
hints). The Python implementation and tests are independently written, with the
gateway's own PostgreSQL accounting and conservative retry policy retained.

2026-09-24 development run: 446 Python tests passed (including PostgreSQL, 40
cross-protocol text cases and five SDK tests), 12 Node UI tests passed. One upstream google-genai Python
deprecation warning remains. Existing Chrome OAuth browser smoke and Python package
build (`--no-isolation`, because ensurepip is absent) passed. New Chrome admin E2E
covers customer/key CRUD, exact quota, provider/credential/model mapping, playground,
Kiot profile, XSS safety, mobile and logout. No real account, Kiot
key or third-party provider key was used.
Independent whole-branch review found ten Important issues; they were addressed in
one implementer RED/GREEN fix pass, not a second reviewer approval. See
`gateway-review-resolution.md` for evidence and remaining operational limitations.

Reproduce SDK checks after installing `requirements/gateway-sdk-tests.lock`:

```bash
.venv/bin/python -m pytest tests/gateway/sdk -q
```

Tests use dedicated loopback PostgreSQL only. See `docs/gateway-operations.md` and
`scripts/gateway_testdb.py` for test-server configuration; a missing DB is an error,
not a silently skipped integration test.
