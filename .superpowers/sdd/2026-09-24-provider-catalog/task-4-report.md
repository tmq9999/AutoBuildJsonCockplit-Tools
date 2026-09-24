# Task 4 report

Implemented durable catalog operations, idempotency claims, fenced ownership,
heartbeat, cancellation, dispatch marking, and safe operation error codes.

## TDD evidence

- RED: `.venv/bin/python -m pytest tests/gateway/integration/test_provider_operations.py -q` failed with `ModuleNotFoundError` because `catalog.operations` did not exist.
- GREEN: focused operation tests pass (`4 passed`); sources/schema integration tests pass (`17 passed` combined).
- Full suite: `.venv/bin/python -m pytest -q` → `656 passed, 1 warning`.
- Ruff: `.venv/bin/ruff check ...` → `All checks passed!`.

## Files

- `autobuild_json/gateway/catalog/operations.py`
- `autobuild_json/gateway/errors.py`
- `tests/gateway/integration/test_provider_operations.py`

## Self review and concerns

Completed client claims are read before source context validation, so replay works
after disable. Active requests coalesce by digest; mismatched payloads conflict.
Claims increment a generation and terminal writes use owner/generation/deadline
compare-and-set. Cancellation preserves running evidence and blocks terminal writes.
No raw provider errors or secret data are persisted. Recovery of expired running
claims remains Task 9 scope; no network or customer ledger behavior was added.

## Fix round 1

Added claim-time comparison of stored expected stamp with live context and
transitioned stale or expired queued rows to terminal `stale`. Client-claim
insert conflicts now reselect and validate digest, allowing concurrent same-ID
replay. Result data is bounded and screened for secret, raw, and body fields;
repeat running cancel requests leave version unchanged. Added concurrent
enqueue/claim, active payload conflict, stale/expiry, result, and lost-generation
coverage.

- RED: `.venv/bin/python -m pytest tests/gateway/integration/test_provider_operations.py -q` → `5 failed, 3 passed`.
- GREEN: same command → `10 passed`.
- Full suite: `.venv/bin/python -m pytest -q` → `662 passed, 1 warning`.
- Ruff focused check → `All checks passed!`.
- Warning is the pre-existing third-party `google.genai.types` Python 3.14
  deprecation warning; no dependency edits were made.
