# Task 1 implementation report

## Scope

Implemented phase-aware, redacted transport failure classification and one safe
route retry for proxy connection failures proven to happen before response
headers. Stream/response failures remain non-retryable, and retry cleanup
closes the current route resource context before the next route is selected.

## Tests and commands

- `python3 -m pytest tests/gateway/unit/test_gateway_transport.py tests/gateway/unit/test_engine_resource_order.py -q`
  - **19 passed**.
- `python3 -m pytest tests/gateway/unit/test_gateway_transport.py tests/gateway/unit/test_engine_resource_order.py tests/gateway/integration/test_gateway.py -q`
  - **19 passed, 8 errors**. The integration fixture could not start because
    this environment has no configured PostgreSQL test binary or
    `AUTOBUILD_TEST_DATABASE_URL` (`ValueError: Provide --postgres-bin or a
    dedicated test database URL`).
- `python3 -m pytest tests/gateway/unit -q`
  - **767 passed, 5 failed, 11 errors**. The failures/errors are environment
    setup/dependency issues unrelated to this change: missing `uvicorn`,
    `greenlet`, PostgreSQL test binary/URL, and one maintenance CLI setup
    failure caused by unavailable startup dependencies.
- `ruff check autobuild_json/gateway/errors.py autobuild_json/gateway/engine.py autobuild_json/gateway/transport/http.py autobuild_json/gateway/transport/websocket.py tests/gateway/unit/test_gateway_transport.py tests/gateway/unit/test_engine_resource_order.py`
  - **All checks passed**.
- `python3 -m compileall -q autobuild_json tests/gateway/unit/test_gateway_transport.py tests/gateway/unit/test_engine_resource_order.py`
  - **Passed**.

## Changes

- Added internal `TransportFailure` classification with `before_response` /
  `after_response`, proxy-used state, and `safe_retry` only for a proxied
  pre-response failure. Public error serialization remains unchanged and raw
  exception text is not retained.
- Marked stream read/reset failures as after-response and non-retryable.
- Applied the same ambiguity rule to WebSocket I/O after handshake; handshake
  connection failures can be classified pre-response but WebSocket stream
  resets cannot retry.
- Added a bounded retry branch in `Engine.prepare()` that marks the failed
  pre-generation attempt rejected, settles any zero-use budget reservation,
  closes provider/proxy resources, then proceeds only to an untried route.
- Added regression tests for pre-response proxy connect retryability,
  post-chunk reset non-retryability, and release-before-retry ordering.

## Concerns

- PostgreSQL-backed integration tests were not runnable here because the
  required dedicated test database configuration/binary is unavailable.
- Full unit execution also encountered pre-existing environment dependency
  gaps (`uvicorn`, `greenlet`) outside the touched gateway transport paths.

## Follow-up review fix

- Added a per-`prepare()` `transport_retry_used` fence. A pre-response proxied
  transport failure can now consume at most one route retry; a later failure
  surfaces without walking the remainder of a Codex pool.
- Added a three-route regression asserting only two adapter opens and complete
  first-attempt resource cleanup before the single retry.
- Follow-up verification: focused transport/resource tests **14 passed**;
  Ruff and compileall **passed**.
