# Task 1 implementation report

## Changes

- Changed `ProxyManager.acquire(..., wait=False)` to raise the internal `ProxyCapacityError` when an eligible endpoint lease is busy, allowing `Engine.route_resources` to take its existing provider-release/retry path.
- Disabled static endpoint resources are skipped as permanent `proxy_not_ready` failures and do not produce capacity signals.
- Added bounded (2048-sample) proxy wait-duration telemetry with p50/p95/last wait values to `ProxyManager.snapshot()`; durations are measured around the real `wait_for_capacity` operation and expose no endpoint identity.
- Updated unit coverage for the internal busy-capacity signal and disabled endpoint behavior.
- Added a real-composition Engine regression using `ProxyManager`, `MemoryLeaseStore`, and a deterministic lease-store double; it verifies provider admission exits before proxy waiting and the request retries successfully after the lease is released.

## Tests / commands

- `/usr/bin/python3 -m compileall -q autobuild_json tests/gateway/unit/test_proxy_manager.py tests/gateway/integration/test_proxy_engine_capacity.py` — passed.
- `git diff --check` — passed.
- `pytest tests/gateway/unit/test_proxy_manager.py tests/gateway/unit/test_engine_resource_order.py tests/gateway/integration/test_proxy_engine_capacity.py -q` — not runnable in this environment (`pytest: command not found`).
- Attempted `/usr/bin/python3 -m pytest ...` — Python environment has no pytest module.
- Attempted temporary virtualenv setup — unavailable because `ensurepip`/`python3-venv` is not installed.

## Concerns

- Focused and quota integration suites could not be executed because the workspace image lacks pytest and venv tooling; syntax compilation and diff checks pass.
- Existing `Engine.route_resources` compatibility sleep fallback remains unchanged for proxy implementations without `wait_for_capacity`; the real `ProxyManager` path uses explicit bounded waiting.

## Review fix round 1

- Changed expired proxy-capacity handling in `Engine.route_resources` to raise `deadline_exceeded` (504, request stage), preserving the authoritative request deadline error instead of translating it to `proxy_not_ready`.
- Added a unit regression proving provider admission is released before the proxy-capacity expiry is surfaced.
- `/usr/bin/python3 -m compileall -q autobuild_json tests/gateway` — passed.
- `git diff --check` — passed.
- Pytest remains unavailable in the environment; see the test tooling concern above.

## Review fix round 2

- Preserved `ProxyCapacityError` as the class-based internal Engine retry signal and retained `code="proxy_capacity"`, while restoring its exception string to `proxy_not_ready` for direct manager/quota callers and public redaction compatibility.
- Added an assertion covering the internal-code/public-message split.
- Focused command (with the bundled PostgreSQL 18 runtime and required library path):
  `LD_LIBRARY_PATH=/home/mquangprovip0503/Work/Code/AutoBuildJsonCockplit-Tools/.deps/gateway-pg18/root/usr/lib/x86_64-linux-gnu AUTOBUILD_TEST_POSTGRES_BIN=/home/mquangprovip0503/Work/Code/AutoBuildJsonCockplit-Tools/.deps/gateway-pg18/root/usr/lib/postgresql/18/bin /home/mquangprovip0503/Work/Code/AutoBuildJsonCockplit-Tools/.venv/bin/pytest tests/gateway/unit/test_proxy_manager.py tests/gateway/unit/test_engine_resource_order.py tests/gateway/integration/test_proxy_engine_capacity.py tests/gateway/integration/test_codex_quota.py -q`
- Result: `45 passed in 10.24s`.
