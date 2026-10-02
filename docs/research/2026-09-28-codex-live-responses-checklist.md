# Codex Responses live repair

- [x] Identify the actual Codex request envelope rejected by the gateway.
- [x] Reproduce and correct request metadata/additional-tools decoding in the live worktree.
- [x] Confirm a real text-only Codex CLI request through LAN on final clean runtime.
- [x] Record the earlier complete real Codex tool roundtrip on final code with the model pinned.
- [ ] Reconfirm tool reliability after the latest restart; the 10:55 ICT recheck was interrupted with unknown usage.
- [x] Add focused regressions for observed tool-stream/replay failures, then fix.
- [x] Review adapter boundaries and validate without touching unrelated changes.
- [x] Synchronize task changes to the main checkout.
- [x] Run complete relevant tests on a disposable PostgreSQL fixture; record all limitations.
- [x] Finish live diagnostics and record final evidence; leave LAN running and Cockpit untouched.
- [ ] Record full main-checkout test results (worktree-wide run lacked CheckLive dependency).

No provider secrets or raw provider bodies belong in this document. Live tests use
disposable customers/keys and revoke/disable them while retaining audit history.

## Follow-up under restricted execution

- [x] Recheck saved CLI probe without exposing raw error output; the probe omitted
  its key environment variable and does not explain the preceding live failure.
- [x] Establish execution limits: network/DNS and user service control are denied.
- [x] Reproduce lost upstream error codes before `response.created`, then repair.
- [x] Improve allowlisted CLI failure diagnostics without printing raw messages.
- [x] Run focused offline tests and static checks in both checkouts; broader
  verification has environment/setup failures and a cancellation-test hang.
- [ ] Reload gateway and rerun real tool acceptance when network/service access
  is available; never stop or restart Cockpit/tunnel port 41681.

Verification: focused Responses/native-tool tests 96 passed; the complete
gateway suite passed 1,838 tests with one dependency deprecation warning. A
fresh text-mode Codex CLI acceptance through `http://192.168.134.128:8788/v1`
exited 0, matched `LIVE_GATEWAY_CODEX_OK`, recorded one completed request with
usage and charge, and cleaned up its disposable key/customer. An earlier tool
run exited 0, ran exactly one command with matching output, matched
`LIVE_GATEWAY_CODEX_TOOL_OK`, and settled both client requests with real usage.
The latest 10:55 ICT tool recheck exited 1 and remains `usage_pending` with
`interrupted` reason and retained hold; it was not retried, refunded, or called
a pre-generation rejection. Full evidence and limitations are in the research
report.
