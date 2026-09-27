# Bounded admission live verification (2026-09-27)

## Scope and safety

The live gateway was restarted with the feature worktree on `PYTHONPATH`, the configured provider/profile and twenty configured HTTP egress proxies unchanged, and inference admission capacity 100. The listener reported `{"status":"ok"}` on `GET /health` (HTTP 200). The admin status endpoint returned HTTP 200 and reported admission capacity 100 with zero inflight/queued requests after cleanup. The service was restored under the user systemd unit after verification.

No credentials, API keys, OAuth material, account identities, proxy credentials, raw upstream errors, or raw benchmark artifacts are included here. Temporary customer/key records were revoked/deleted in the benchmark's `finally` path (100/100 key revocations).

## Offline gates (before live work)

* `.venv/bin/python -m pytest -q`: **881 passed, 35 failed, 903 errors**. The errors are setup failures because this worktree does not contain the required dedicated PostgreSQL 17 test binaries; failures are existing gateway assertions reached after partial setup. Exact command/output was captured before live work.
* `ruff check autobuild_json tests`: **1 error**, `tests/gateway/unit/test_admission.py:22` (`F841`, unused `ticket`).
* `.venv/bin/python -m compileall -q autobuild_json`: exit **0**.

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

The 100-user burst exercised real non-streaming `/v1/responses` requests and verified non-empty output and usage for each successful response. A separate real streaming request was not run in this operational window; therefore streaming settlement and zero-leak evidence are **not claimed** here. Follow-up should run one temporary-key stream and inspect the persisted ledger plus admission/proxy snapshots before declaring streaming verified.

## Rollback/cleanup

The original configured proxy/provider profile was preserved. Temporary keys were revoked and the temporary customer removed by the benchmark cleanup path. The live service was left healthy on the public listener (`GET /health` HTTP 200).
