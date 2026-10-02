# Real Codex CLI Responses verification — 2026-09-28

## Failure and repair

The Responses stream error boundary was also repaired: an upstream parser or
transport failure is now encoded as one terminal `response.failed` event with a
safe code. Before this fix, the codec attempted to feed the error into the
lifecycle collector (which could raise `missing_start`) or emitted a top-level
non-terminal `error` event. Codex then reached EOF and reported
`stream closed before response.completed`. The failure path keeps usage unknown
and the request pending for reconciliation; it never fabricates completion.

Codex CLI 0.157.1 sent native `client_metadata`, `service_tier`, and
`additional_tools` input items. The gateway's request allowlist rejected that
envelope with `unsupported_feature` before inference. Fixing that envelope alone
was insufficient: the real provider then emitted `custom_tool_call`, which the
Responses stream parser also rejected. Its native completion metadata changes
between item-added and item-done and must survive replay.

The independently implemented repair preserves the bounded native tool item,
grammar-input deltas, completion metadata, and custom-tool-result replay on the
Codex adapter. Call ID/name/input invariants, provider fences, tool limits, and
real usage settlement remain enforced. The Cockpit Responses request/response
translators were inspected as a behavioral reference; no reference credentials
or source were incorporated.

## Exact command (run from repository root)

```bash
AUTOBUILD_LIVE_VERIFY=1 \
AUTOBUILD_LIVE_CODEX_MODE=text \
AUTOBUILD_LIVE_RUNTIME_ROOT="$PWD/.worktrees/bounded-admission-capacity" \
.venv/bin/python tests/gateway/live_codex_cli.py

AUTOBUILD_LIVE_VERIFY=1 \
AUTOBUILD_LIVE_CODEX_MODE=tool \
AUTOBUILD_LIVE_RUNTIME_ROOT="$PWD/.worktrees/bounded-admission-capacity" \
.venv/bin/python tests/gateway/live_codex_cli.py
```

The verifier invokes installed `codex exec --ephemeral --json --sandbox read-only -m gpt-6-astra`
in a disposable empty directory. It changes only per-invocation provider options
to use `http://192.168.134.128:8788/v1`, disables CLI request/stream retries and
the unrelated configured MCP server, and supplies the disposable gateway key
through an environment variable (never argv or printed config). Admin login uses
the private runtime config, real cookie, matching Origin, and CSRF token.

The tool prompt requests exactly one `printf 'GATEWAY_TOOL_PROBE\n'`, then the
literal answer `LIVE_GATEWAY_CODEX_TOOL_OK`. No repository file access, network
tool call, or delegation is requested. The script checks actual command output
and exit code, the final marker, and completed per-request ledger records.

## Earlier successful live results after removing diagnostics and restarting

Both runs below used the final parser code and the model-pinned verifier on
Codex CLI 0.157.1. Temporary status-only diagnostic logging was removed before
the restart and these calls. This is a fresh result, not reused earlier evidence.

Text: CLI exit 0, final answer matched `LIVE_GATEWAY_CODEX_OK`, zero tool commands.
One completed request, input 10,615 / output 10 tokens; charged 10,625,000,000
micro-units.

Tool: CLI exit 0, exactly one successful command with matching output, final
answer matched `LIVE_GATEWAY_CODEX_TOOL_OK`. Two distinct client requests settled:

| Turn | Input | Output | Cached input (subset) | Charged micro-units |
|---|---:|---:|---:|---:|
| Tool call | 10,649 | 59 | 8,320 | 10,708,000,000 |
| Tool result and final answer | 10,951 | 11 | 8,320 | 10,962,000,000 |

The final text run also contained one explicitly rejected pre-generation provider
attempt, followed by a completed attempt for that same client request; it was
not an extra billable generation and its charged amount was zero. The verifier
groups attempts by request and rejects incomplete/usage-pending requests.

Disposable keys were revoked and customers soft-disabled after both final runs.

## Earlier recheck — 2026-09-28, 10:55 ICT

The earlier successful tool run above is historical evidence, not the latest
acceptance result. After restarting the serving worktree, text mode passed at
10:55:30 (exit 0, marker `LIVE_GATEWAY_CODEX_OK`, one completed request,
10,615 input / 10 output, charged 10,625,000,000 micro-units). Tool mode at
10:55:52 exited 1 before any command completed and produced no final marker.
Its request is `usage_pending`, attempt status `started`, stored reason
`interrupted`, usage absent, charged 0, and held 1,128,000,000,000 micro-units.
Zero charge does not prove that no upstream generation occurred. The temporary
key was revoked and customer soft-disabled; no retry, refund, or data deletion
was performed.

The old verifier discarded stderr and reported no useful client code. It now
emits fixed diagnostic categories and held quota without raw messages,
credentials, or provider bodies; nine offline regression cases cover this
boundary. Nearby quota-refresh logs contain `proxy_error`, but correlation to
this request is not established. The failure boundary remains unresolved, so
no parser, provider, proxy, or retry-policy fix is claimed.

These calls originated on the gateway machine using its LAN IP, not on the
external host. They used a real provider through the configured proxy pool.
Cockpit `127.0.0.1:41681` was left running and was not used as the final test
upstream. The gateway is managed by `autobuild-local-gateway.service` with public
API on `192.168.134.128:8788` and private admin on `127.0.0.1:8787`.

## Follow-up — 2026-09-28, 13:32 ICT

Before execution became restricted, the safe-upstream-code patch passed 59
focused tests and static checks on the runtime worktree. Only
`autobuild-local-gateway.service` was restarted; health returned `ok` and the
Cockpit listener at 41681 retained the same process. The subsequent live tool
attempt exited 1 with no command or final marker and `requests: []` in the
verifier's attempt-report page. Its disposable key was revoked and customer
disabled. That empty page does **not** establish that no HTTP request reached
the gateway. Raw CLI output was not retained, and its cause remains unresolved.

A separate probe to an unused loopback port omitted the key environment
variable and produced a missing-variable error. That probe does not diagnose
the real attempt, whose script does supply the variable. A follow-up probe
under the restricted execution environment failed before JSON events were
produced; it cannot establish provider behavior either.

Offline inspection found another concrete parser defect: upstream errors before
`response.created` hit `missing_start` before safe-code extraction. Six failing
regressions reproduced lost `unsupported_feature`/`rate_limited` codes across
flat, nested, and `response.failed` envelopes. The fix moves error handling ahead
of the start check while retaining the post-terminal guard. Twelve cases cover
safe, unknown, and malformed codes and assert that no retry becomes eligible.
The CLI verifier now classifies admission/auth/quota errors and HTTP/local
environment hints, matching whole code tokens rather than arbitrary substrings.

Both changes are synchronized to the runtime worktree but **have not been
reloaded or verified live**. Current execution denies socket creation, DNS,
and user systemd control. No tunnel was stopped, no provider request was retried,
and no runtime quota or data was reset in this follow-up.

Focused runtime-worktree verification:

```bash
/home/mquangprovip0503/Work/Code/AutoBuildJsonCockplit-Tools/.venv/bin/pytest -q \
  tests/gateway/unit/test_codex_native_tools.py \
  tests/gateway/unit/test_responses_stream_failures.py \
  tests/gateway/unit/test_responses.py tests/gateway/unit/test_responses_reasoning.py \
  tests/gateway/unit/test_codex_websocket_transport.py \
  tests/gateway/unit/test_codex_websocket_boundary.py \
  tests/gateway/unit/test_live_codex_cli.py --tb=short
```

Result: **175 passed**. Ruff, compileall and diff checks passed in both checkouts.
The whole main-checkout suite stopped at
`test_account_pool.py::test_policy_cas_and_audit_are_atomic`: without explicit
PostgreSQL-bin configuration the test fixture is unavailable; with the correct
PostgreSQL 18 binaries it fails creating a socket with `Operation not permitted`.
This is not a current integration/full-suite pass.

## Scope and limitations

After the terminal-error fix and service reload, a fresh real Codex CLI tool
acceptance passed through `http://192.168.134.128:8788/v1`: exit 0, one actual
command with matching output, final marker matched, and two completed ledger
requests with usage and zero held quota. This is one live acceptance run, not a
claim that every long-running or provider-side interruption is eliminated.

The earlier successful run is evidence for text plus a real native custom-tool
roundtrip on this CLI version/model. The latest tool recheck is unresolved, so
this is not a current reliability pass or full Cockpit parity claim. Long-session
compaction, all tool/media types, WebSocket behavior, and public TLS deployment
were not verified by these calls. Plain HTTP is for the requested private LAN
test only. No test changed runtime provider/proxy policy or deleted runtime data.

## Changed implementation

- `gateway/contracts.py`: bounded metadata and native tools capability detection.
- `gateway/protocols/openai_responses.py`: native request/replay validation and SSE encoding.
- `gateway/protocols/common.py`: bounded custom-tool accumulation and final-item checks.
- `gateway/providers/responses_events.py`: native tool SSE parsing.
- `gateway/providers/codex.py`, `preflight.py`: tier normalization and wire fencing.
- Focused regressions in `tests/gateway/unit/test_codex_native_tools.py` and
  `test_responses.py`; live verifier in `tests/gateway/live_codex_cli.py`.

Test-suite results are recorded separately from the live provider evidence.

## Schema and verification evidence

The official [Responses create reference](https://developers.openai.com/api/reference/resources/responses/methods/create)
documents `additional_tools`, custom tool calls/results, `caller`, `async`, and
assistant phase replay. The [function calling guide](https://developers.openai.com/api/docs/guides/function-calling)
documents custom-tool input. This patch does not claim full Responses parity.

Final focused command (run from either checkout with the main venv):

```bash
/home/mquangprovip0503/Work/Code/AutoBuildJsonCockplit-Tools/.venv/bin/python -m pytest \
  tests/gateway/unit/test_codex_native_tools.py \
  tests/gateway/unit/test_responses.py tests/gateway/unit/test_responses_reasoning.py -q --tb=short
```

Result: 96 passed. Gateway suite: 1,838 passed with one Google SDK deprecation
warning, using disposable PostgreSQL 18. Ruff, compileall and diff checks passed.

An additional bare `python -m pytest -q --tb=short` from the live worktree found
35 OAuth/workbench failures (1,970 passed) because that worktree has no
`.deps/Check-Account-ChatGPT`. Failing tests were in `test_checklive_adapter.py`,
`test_dependency_setup.py`, `test_end_to_end.py`, `test_login_bootstrap.py`, and
`test_setup.py`; all failed at the dependency-loading/configuration boundary.
The full main-checkout run uses its existing dependency instead, without copying,
resetting, or deleting operator state.
