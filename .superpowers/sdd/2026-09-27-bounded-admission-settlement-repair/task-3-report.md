# Task 3 implementation report

## Scope

Resource waits now consume one request deadline across provider and proxy
stages. Provider admission is released before proxy-capacity waiting; no limit
was increased and direct fallback was not introduced. Safe stage telemetry is
additive and redacted: provider/proxy/upstream outcomes distinguish deadline
expiry, cancellation, rate limiting, and other failures. Queue/resource wait
samples remain bounded by the existing 2048-entry deques.

## Changed files

- `autobuild_json/gateway/engine.py`: shared remaining-budget wait, provider /
  proxy / upstream stage attribution, bounded proxy waiter, and primary error
  preservation.
- `autobuild_json/gateway/admission.py`: bounded queue-wait fields and per-stage
  terminal outcome counters.
- `autobuild_json/gateway/providers/limits.py`: provider expiry/cancel/rate-limit
  counters in snapshots.
- `autobuild_json/gateway/proxy/manager.py`: bounded proxy waiter outcome
  counters and cancellation/expiry accounting.
- `autobuild_json/gateway/errors.py`: `provider` is an allowed redacted stage.
- Tests: actual engine stage/outcome regressions and PostgreSQL deadline,
  cancellation, cleanup, and bounded-sample coverage.

## TDD evidence

The new unit tests were first run against the pre-change implementation and
failed (`3 failed`): provider deadline was reported as `upstream`, cumulative
provider+proxy expiry was reported as `request`, and stage snapshot counters did
not exist. During implementation, an intermediate run exposed a double-count /
stage-overwrite bug caused by nested exception scopes; explicit
`provider_waiting` / `proxy_waiting` ownership flags now ensure a post-yield
deadline, 429, or cancellation is counted once as `upstream`.

## Verification

Using the bundled PostgreSQL 18 binary and repository virtualenv:

```text
pytest tests/gateway/unit/test_engine_resource_order.py \
  tests/gateway/unit/test_admission.py tests/gateway/unit/test_proxy_manager.py \
  tests/gateway/integration/test_provider_admission_wait.py \
  tests/gateway/integration/test_final_capacity_regressions.py \
  tests/gateway/integration/test_proxy_engine_capacity.py -q
75 passed in 15.69s
```

The PostgreSQL regressions include real provider-then-proxy cumulative deadline
expiry and cancellation. They assert zero active `provider_admissions` and zero
owned `proxy_leases` after both paths. Focused snapshots assert provider/proxy
wait percentiles, bounded sample history, and stage-specific expiry/cancel/
rate-limit counters. Ruff, compileall, and `git diff --check` all pass.

