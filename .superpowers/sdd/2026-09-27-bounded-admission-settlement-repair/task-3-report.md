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

## Review fix round 1

The review found two telemetry gaps. Provider waits could be cancelled or
deadline-expired inside the first per-provider asyncio/SQL lock acquisition
before the old `wait_started` marker was initialized. Terminal adapter and
stream failures were also handled by `prepare()` / `PreparedCall.events()`
after the route context had been entered, so route-resource exception
telemetry could not observe them. The fix starts every `wait=True` provider
episode before the first bounded acquisition (including successful slow lock
waits), and adds one idempotent upstream-outcome recorder shared by route,
prepare, and stream owners. Retry attempts are not counted until their
terminal outcome; repeated close/finalization does not duplicate a sample.

Review regression evidence:

```text
# Before the fix (review RED): provider lock cancellation/deadline left
# cancelled_waits / _waits empty; prepare/PreparedCall terminal upstream
# outcomes left upstream counters at 0.

pytest tests/gateway/unit/test_engine_resource_order.py \
       tests/gateway/unit/test_engine_admission.py -q
25 passed in 0.79s

pytest tests/gateway/integration/test_provider_admission_wait.py \
       tests/gateway/integration/test_final_capacity_regressions.py \
       tests/gateway/integration/test_proxy_engine_capacity.py -q
26 passed in 16.84s
```

The first matrix includes actual `Engine.prepare()` retry exhaustion and an
actual `PreparedCall.events()` stream exception asserting exactly one upstream
rate-limit sample. The PostgreSQL matrix includes provider row-lock deadline
and cancellation tests asserting one wait sample/counter and zero active
provider admission or owned proxy lease rows.

## Review fix round 2

The second review found that the broad `prepare()` `GatewayError` handler could
label pre-I/O policy/configuration failures (for example the attribution
`revalidate()` fence) as upstream failures. `upstream_io_started` now gates
prepare-side outcome recording; only adapter/transport I/O and stream owners
can increment upstream terminal counters. The original safe error code/stage is
preserved for pre-I/O failures.

```text
pytest tests/gateway/unit/test_engine_resource_order.py \
       tests/gateway/unit/test_engine_admission.py -q
26 passed in 0.56s

ruff check <changed files>
All checks passed!
python -m compileall -q <changed files>
Passed (no output).
git diff --check
Passed (no output).
```

The new regression drives `Engine.prepare()` with a second-fence
`invalid_state` policy failure, verifies the adapter was never opened, keeps the
original `policy` stage, and asserts upstream terminal counters remain zero.
