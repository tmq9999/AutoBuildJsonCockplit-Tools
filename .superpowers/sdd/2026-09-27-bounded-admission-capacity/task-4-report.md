# Task 4 report: classify and retry proxy capacity safely

## Implemented

- Added an internal `ProxyCapacityError` (`proxy_capacity`) signal for a busy local lease. It is not a public `GatewayError` code and therefore cannot expose endpoint details.
- Added `wait=True` to `ProxyManager.acquire()`. Local capacity waits are bounded by the existing 10-second acquisition cap (or the request deadline), while permanent Kiot key/provider failures remain immediate. Existing endpoint exclusivity, key rotation, lease cleanup, and heartbeat behavior are preserved.
- Added proxy wait/capacity counters to `ProxyManager.snapshot()` for capacity snapshots.
- Updated `Engine.route_resources()` to use provider wait mode and proxy wait mode. A capacity signal exits both contexts before retrying, so no provider admission is held while waiting for a proxy lease. Deadline exhaustion returns a public `proxy_not_ready` error.
- Added focused tests for bounded static-pool capacity signaling, cancellation cleanup, and provider-release ordering around proxy retries.
- Follow-up classification fix: only a failed local lease claim marks retryable capacity. Kiot `proxy_not_ready` and `kiot_unavailable` now surface immediately; invalid keys are disabled while rotation continues, and mixed invalid/busy pools correctly report retryable capacity.

## Verification

`../../.venv/bin/python -m pytest tests/gateway/unit/test_proxy_manager.py tests/gateway/unit/test_engine_resource_order.py -q` — **14 passed** (0.59s; emitted only the environment `sys.prefix` runtime warnings).

After the classification fix, the same focused command reports **16 passed** (0.61s).

`git diff --check` — passed.

Focused pytest command requested by the brief:

```
pytest tests/gateway/unit/test_proxy_manager.py tests/gateway/unit/test_engine_resource_order.py -q
```

`../../.venv/bin/python -m pytest tests/gateway/unit/test_proxy_manager.py tests/gateway/unit/test_engine_resource_order.py tests/gateway/integration/test_codex_quota.py -q` — **14 passed, 25 errors**. All 25 errors occur during the integration fixture setup because the configured PostgreSQL 17 test binary is absent (`ValueError: Provide --postgres-bin or a dedicated test database URL`); no test body failures were reported.
