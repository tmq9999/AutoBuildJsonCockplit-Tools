# Task 1 report: pure cache normalization, rates and policy defaults

## RED

Command:

```text
.venv/bin/python -m pytest tests/gateway/unit/test_usage_normalization.py -q
```

Result before implementation: 14 failed. The expected missing-helper failures were `ModuleNotFoundError` for `autobuild_json.gateway.metering.usage` and `ImportError` for `weighted_usage_micro`/`hold_micro`.

## GREEN

Focused command:

```text
.venv/bin/python -m pytest tests/gateway/unit/test_usage_normalization.py tests/gateway/unit/test_units.py tests/gateway/unit/test_costs.py tests/gateway/unit/test_key_policy.py -q
```

Result: `42 passed in 0.13s`.

Compatibility provider checks (including existing OpenAI, Anthropic and Gemini payload tests): `60 passed in 0.22s`.

Lint command:

```text
.venv/bin/ruff check autobuild_json/gateway/metering autobuild_json/gateway/identity autobuild_json/gateway/routing/records.py autobuild_json/gateway/providers
```

Result: `All checks passed!`.

Full gateway suite:

```text
.venv/bin/python -m pytest -q
```

Result: `933 passed, 1 warning in 147.78s`. The warning is the existing `google.genai` deprecation warning.

## Files changed

- Added `autobuild_json/gateway/metering/usage.py` with strict provider normalizers for OpenAI Chat, OpenAI Responses, Codex, Anthropic and Gemini.
- Added cache-aware weighted usage and worst-case hold arithmetic to `metering/units.py`.
- Added nullable cache-rate fields to `ModelRate`, `ModelConfig`, `Admission` and `RouteSnapshot` while preserving old constructor positions.
- Routed existing provider normalizers through the canonical helper with upstream error compatibility.
- Added `tests/gateway/unit/test_usage_normalization.py` covering disjoint cache buckets, strict counts, cache subset validation, Gemini reasoning totals, zero cache rates and overflow.

## Self-review

- Cache buckets are charged as `input - cached_read - cached_write`, so inclusive provider input is not double-counted.
- Cache rate `None` inherits input rate; explicit zero remains free. Hold uses the maximum possible input bucket rate.
- Required fields are not inferred from text or other payload fields; malformed, negative, boolean, noninteger and nonfinite counts are rejected.
- Existing provider, cost and ledger tests remain green.

## Concerns

No known Task 1 concerns. Database persistence and effective-rate snapshot wiring are intentionally deferred to Task 2.
