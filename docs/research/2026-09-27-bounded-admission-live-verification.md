# Bounded admission live verification (2026-09-27)

## Scope and safety

The live gateway was restarted with the feature worktree on `PYTHONPATH`, the configured provider/profile and twenty configured HTTP egress proxies unchanged, and inference admission capacity 100. The listener reported `{"status":"ok"}` on `GET /health` (HTTP 200). The admin status endpoint returned HTTP 200 and reported admission capacity 100 with zero inflight/queued requests after cleanup. The service was restored under the user systemd unit after verification.

No credentials, API keys, OAuth material, account identities, proxy credentials, raw upstream errors, or raw benchmark artifacts are included here. Temporary customer/key records were revoked/deleted in the benchmark's `finally` path (100/100 key revocations).

## Offline gates (before live work)

* `.venv/bin/python -m pytest -q`: **881 passed, 35 failed, 903 errors**. The errors are setup failures because this worktree does not contain the required dedicated PostgreSQL 17 test binaries; failures are existing gateway assertions reached after partial setup. Exact command/output was captured before live work.
* `ruff check autobuild_json tests`: **1 error**, `tests/gateway/unit/test_admission.py:22` (`F841`, unused `ticket`).
* `.venv/bin/python -m compileall -q autobuild_json`: exit **0**.

After the initial gate, the unused admission context binding in
`tests/gateway/unit/test_admission.py:22` was removed (test-only; no runtime
behavior change). Fresh verification: `ruff check autobuild_json tests` reports
**All checks passed!** and `pytest -q tests/gateway/unit/test_admission.py`
reports **7 passed**.

## Real burst evidence

`data/proxy_load_verify.py burst` used 100 distinct temporary API keys, the configured `gpt-6-astra` route, real `/v1/responses`, and no client retry. Raw output/artifact was deleted after redacted extraction.

| Metric | Result |
|---|---:|
| submitted users / attempts | 100 / 100 |
| accepted/completed | 94 / 94 |
| expired | 0 |
| genuine upstream errors | 6 (`429`, rate-limited, upstream stage) |
| queue wait p50 / p95 | not emitted by burst client; admission snapshot was clean after run |
| end-to-end p50 / p95 | 50,537.6 ms / 76,382.76 ms |
| non-empty responses / usage-complete | 94 / 94 |
| input / output / reasoning tokens | 5,264 / 16,421 / 3,812 |
| health probes | 124 HTTP 200; max 2,686.9 ms |
| DB connections max / lock waiters max | 27 / 23 |
| temporary keys revoked | 100 |

The six 429s are upstream/provider rate-limit observations, not admission-capacity claims. This run demonstrates accepted/queued/completed accounting; it does not claim 100 simultaneous upstream generations (the configured provider limit remained 16).

## Streaming and settlement

The initial 100-user burst exercised real non-streaming `/v1/responses` requests and verified non-empty output and usage for each successful response. In the follow-up window, a separate temporary-key streaming request returned HTTP 200 (`text/event-stream`) with 11 SSE events, non-empty output, a usage event, and `response.completed`; temporary key/customer cleanup ran in `finally`.

Post-cleanup SQL observed two active provider-admission rows and two owned proxy-lease rows at the first check. A later read-only check showed the provider rows had expired deadlines and one remaining endpoint lease had a future deadline, indicating background/pre-existing activity rather than a provable leak from the temporary stream. No rows were modified, and zero-leak is not claimed.

## Rollback/cleanup

The original configured proxy/provider profile was preserved. Temporary keys were revoked and the temporary customer removed by the benchmark cleanup path. The live service was left healthy on the public listener (`GET /health` HTTP 200).

## Follow-up review attempt

The subsequent review-window restart initially reported `{"status":"unavailable"}` because the worktree `data` link had been removed while its PostgreSQL cluster was running. After read-only validation of the original data target, the link was restored and the same service restarted without resetting or creating a database. `/health` then returned HTTP 200.

A real temporary-key streaming `/v1/responses` request completed with HTTP 200 and `text/event-stream`; the redacted verifier observed 11 SSE events, non-empty output text, a usage event, and `response.completed`. Cleanup revoked the temporary key and deleted its temporary customer in `finally`. A direct post-cleanup database check reported 2 active provider-admission rows and 2 owned proxy-lease rows. Read-only follow-up showed the provider deadlines were expired and one endpoint lease remained future-dated, consistent with background/pre-existing activity; no rows were modified and zero-leak cannot be claimed.

The admin status process is separate from the serving engine, so its capacity counters remain zero/unsampled for request traffic; queue wait p50/p95, provider-active maximum, and proxy-lease maximum were therefore not available from that endpoint. They are not inferred or presented as zero. The earlier burst metrics remain the only acceptance measurement.
