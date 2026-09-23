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

- Text and basic function/tool calls are the implemented core. Vision, structured
  outputs, signed thinking blocks, prompt cache controls and provider-specific tool
  features are rejected when not representable. No silent parameter dropping is
  intended; unsupported combinations require an explicit error and regression test.
- Responses background/store=true/compact/retrieval/WebSocket are unsupported.
  Continuation handles are tenant/key/model/route/credential scoped, encrypted and
  enabled only for a native route with that capability. Chat-backed Responses
  requires complete history in each request.
- Native Anthropic/Gemini count endpoints are available; translated routes lacking
  a trustworthy counter return unsupported. Counts share the key's RPM/concurrency
  gate and do not charge generation quota. Hard money-budget counting routes require
  a separate counter cost policy and currently fail closed.
- Codex OAuth does not accept an explicit unsupported max_output_tokens cap. The
  route uses a finite operator-configured model bound for reservation. Clients that
  always require this parameter cannot use that Codex route. No live Codex inference
  contract has been verified by this gateway implementation.
- Model discovery via public APIs is filtered by client policy. Private provider
  discovery supports native model-list shapes and stages entries for admin review.
- Public gateway requires a client key for every protocol, including Ollama. No key
  query parameter; Gemini `alt=sse` is an allowed nonsecret query.
- The approved local admin UI is implemented at `/service/`. Existing OAuth workbench
  now also offers explicit direct/fixed/pool/Kiot selections; old default payloads work.

## Verification snapshot

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
