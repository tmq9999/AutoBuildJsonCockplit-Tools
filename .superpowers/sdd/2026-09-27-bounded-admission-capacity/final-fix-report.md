# Final fix wave report

Date: 2026-09-27

## Fixes

* `Engine.route_resources` now claims a proxy non-waiting while the provider
  admission is held. On local proxy capacity exhaustion the provider context
  exits first, then an explicit proxy-capacity wait runs outside both provider
  and proxy leases before retrying. The resource-order regression records
  `provider.exit` before `proxy.wait`.
* Preparation and prepared-call cleanup use nested `try/finally`, so admission
  tickets are released even when provider/proxy/stream cleanup raises. The
  primary cleanup exception remains visible when admission release succeeds.
* Added migration `0016_capacity_snapshots`. Serving engines publish a
  redacted singleton JSON snapshot; admin status reads it across processes,
  marks snapshots older than 30 seconds stale, and only falls back for
  pre-migration databases. No credentials, prompts, identities, or proxy URLs
  are present in the snapshot.
* Provider admission sets PostgreSQL transaction-local `lock_timeout` from the
  remaining request deadline before `FOR UPDATE`; lock-timeout SQLSTATE 55P03
  is classified as `deadline_exceeded` and cannot insert an admission row.
* Admission wait telemetry now uses a `deque(maxlen=2048)`; snapshots sort only
  the bounded rolling sample.
* `scripts/local_gateway.py` preserves `AUTOBUILD_GATEWAY_INFERENCE_CAPACITY`
  when rebuilding the child environment.
* The live verification report now distinguishes the pre-fix separate-admin
  telemetry limitation from the new shared snapshot implementation and keeps
  the measured evidence honest (94/100 non-stream successes plus six upstream
  429s; follow-up stream 200/11 SSE/nonempty/usage/completed; post-cleanup
  active rows/leases non-zero and no zero-leak claim).

## TDD and verification evidence

Focused RED/GREEN fixes were added for provider-before-proxy wait ordering,
cleanup-error admission release, bounded wait history, and cross-process status
snapshot reading. Focused unit verification:

```text
../../.venv/bin/python -m pytest -q \
  tests/gateway/unit/test_engine_resource_order.py \
  tests/gateway/unit/test_engine_admission.py \
  tests/gateway/unit/test_admission.py \
  tests/gateway/unit/test_proxy_manager.py
33 passed in 0.51s (including local-gateway environment propagation and
single-pass `wait=False` proxy-claim regressions)
```

```text
../../.venv/bin/ruff check <changed files>
All checks passed!
../../.venv/bin/python -m compileall -q autobuild_json scripts/local_gateway.py
exit 0
```

PostgreSQL-focused integration command was attempted but is unavailable in
this worktree because `.deps/gateway-pg17/binary/bin/{initdb,pg_ctl}` is absent
and `AUTOBUILD_TEST_DATABASE_URL` is unset:

```text
pytest ...test_provider_admission_wait.py ...test_codex_service_status.py
12 errors during fixture setup (ValueError: Provide --postgres-bin or a dedicated test database URL)
```

The targeted PostgreSQL 18 verification was subsequently run with the
available bundled binaries:

```text
AUTOBUILD_TEST_POSTGRES_BIN=$PGROOT/usr/lib/postgresql/18/bin \
LD_LIBRARY_PATH=$PGROOT/usr/lib/x86_64-linux-gnu \
../../.venv/bin/python -m pytest -q \
  tests/gateway/integration/test_provider_admission_wait.py \
  tests/gateway/integration/test_codex_service_status.py
12 passed in 5.30s
```

The provider row-lock regression now expects the deadline-bound lock timeout
to complete while the blocker is still held (`pending.done()`), awaits the
`deadline_exceeded` error, and verifies no admission row remains after blocker
rollback.

The required `data` symlink was preserved. Commits for this wave are recorded
in the branch history with subjects `fix: close final bounded admission review
gaps`, `test: preserve local gateway capacity configuration`, and `fix: make
proxy non-wait claims single pass`.
The pre-0016 rolling-schema fallback is in `fix: tolerate pre-telemetry admin
schema`.
