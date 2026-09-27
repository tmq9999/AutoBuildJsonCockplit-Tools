# Task 3 report — bounded serving capacity telemetry

Implemented lifecycle-owned capacity telemetry for the serving gateway.

## Changes

- Added idempotent `Engine.start_capacity_publisher()`, coalescing `request_capacity_publish()`, and bounded `stop_capacity_publisher()` shutdown/flush.
- Replaced request-path detached `_publish_capacity()` task creation with dirty-event notifications consumed by one publisher task.
- Started and stopped the publisher from the serving FastAPI lifespan before database/service shutdown.
- Kept admission/provider/proxy wait samples bounded by their existing fixed-size deques and retained p50/p95 snapshots.
- Made admin service status read only the serving snapshot: fresh snapshots are returned directly, snapshots older than 30 seconds are marked stale, and missing/unavailable schema or rows return `serving_snapshot_unavailable`; no idle Engine fallback remains.
- Preserved redaction and health endpoint independence.

## Verification

- `ruff check autobuild_json/gateway/engine.py autobuild_json/gateway/http/app.py autobuild_json/gateway/admin/codex_accounts.py` — passed.
- `pytest tests/gateway/unit/test_capacity_publisher.py -q` — 2 passed (coalescing/join and bounded wait samples).
- `pytest tests/gateway/unit/test_engine_admission.py tests/gateway/integration/test_codex_service_status.py -q` — unit tests passed; PostgreSQL integration tests could not run because the bundled PostgreSQL 17 binary is absent in this worktree (`Provide --postgres-bin or a dedicated test database URL`).

## Concerns

Integration verification requires the repository's PostgreSQL 18/17 test environment. Existing checked-in status tests that expect an idle Engine fallback need to be updated to the new explicit-unavailable contract.
