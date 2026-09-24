import math

import pytest


def test_cache_buckets_are_disjoint():
    from autobuild_json.gateway.metering.usage import normalize_provider_usage
    from autobuild_json.gateway.metering.units import weighted_usage_micro

    inclusive = normalize_provider_usage(
        {"input_tokens": 1000, "output_tokens": 200,
         "input_tokens_details": {"cached_tokens": 600}}, "codex"
    )
    exclusive = normalize_provider_usage(
        {"input_tokens": 400, "output_tokens": 200,
         "cache_read_input_tokens": 600, "cache_creation_input_tokens": 0}, "anthropic"
    )
    assert inclusive == exclusive
    assert weighted_usage_micro(inclusive, 1_000_000, 3_000_000, 100_000) == 1_060_000_000


@pytest.mark.parametrize(
    "provider,payload",
    [
        ("openai_chat", {"prompt_tokens": 1}),
        ("openai_responses", {"input_tokens": 1}),
        ("anthropic", {"output_tokens": 1}),
        ("gemini", {"candidatesTokenCount": 1}),
    ],
)
def test_missing_usage_fields_are_rejected(provider, payload):
    from autobuild_json.gateway.metering.usage import normalize_provider_usage

    with pytest.raises(ValueError, match="invalid_usage"):
        normalize_provider_usage(payload, provider)


@pytest.mark.parametrize("value", [-1, True, 1.5, math.nan, math.inf])
def test_usage_counts_must_be_finite_nonnegative_integers(value):
    from autobuild_json.gateway.metering.usage import normalize_provider_usage

    with pytest.raises(ValueError, match="invalid_usage"):
        normalize_provider_usage(
            {"input_tokens": value, "output_tokens": 1}, "openai_responses"
        )


def test_cache_buckets_cannot_exceed_inclusive_input():
    from autobuild_json.gateway.metering.usage import normalize_provider_usage

    with pytest.raises(ValueError, match="invalid_usage"):
        normalize_provider_usage(
            {"input_tokens": 10, "output_tokens": 1,
             "input_tokens_details": {"cached_tokens": 11}}, "openai_responses"
        )


@pytest.mark.parametrize("provider,payload", [
    ("openai_chat", {"prompt_tokens": 1, "completion_tokens": 1,
                     "prompt_tokens_details": {"cached_tokens": None}}),
    ("codex", {"input_tokens": 1, "output_tokens": 1,
               "input_tokens_details": {"cached_tokens": None}}),
    ("anthropic", {"input_tokens": 1, "output_tokens": 1,
                   "cache_read_input_tokens": None}),
    ("gemini", {"promptTokenCount": 1, "candidatesTokenCount": 1,
                "thoughtsTokenCount": None}),
])
def test_present_null_optional_count_is_rejected(provider, payload):
    from autobuild_json.gateway.metering.usage import normalize_provider_usage

    with pytest.raises(ValueError, match="invalid_usage"):
        normalize_provider_usage(payload, provider)


@pytest.mark.parametrize("provider,payload", [
    ("openai_responses", {"input_tokens": 1, "output_tokens": 1}),
    ("anthropic", {"input_tokens": 1, "output_tokens": 1}),
    ("gemini", {"promptTokenCount": 1, "candidatesTokenCount": 1}),
])
def test_absent_optional_count_remains_zero(provider, payload):
    from autobuild_json.gateway.metering.usage import normalize_provider_usage

    usage = normalize_provider_usage(payload, provider)
    assert (usage.cached_read, usage.cached_write, usage.reasoning) == (0, 0, 0)


def test_gemini_thoughts_are_in_output_and_reasoning():
    from autobuild_json.gateway.metering.usage import normalize_provider_usage

    usage = normalize_provider_usage(
        {"promptTokenCount": 100, "candidatesTokenCount": 20,
         "thoughtsTokenCount": 10, "cachedContentTokenCount": 50}, "gemini"
    )
    assert usage.input_tokens == 100
    assert usage.output_tokens == 30
    assert usage.reasoning == 10
    assert usage.cached_read == 50


def test_weighted_usage_rates_default_and_zero_cache_rates():
    from autobuild_json.gateway.metering.records import Bounds
    from autobuild_json.gateway.metering.units import hold_micro, weighted_usage_micro
    from autobuild_json.gateway.metering.records import Usage

    usage = Usage(100, 20, cached_read=60, cached_write=10)
    assert weighted_usage_micro(usage, 1_000_000, 3_000_000) == 160_000_000
    assert weighted_usage_micro(usage, 1_000_000, 3_000_000, 0, 0) == 90_000_000
    assert hold_micro(Bounds(100, 20), 1_000_000, 3_000_000, 0, 0) == 160_000_000


def test_weighted_usage_rejects_rate_overflow():
    from autobuild_json.gateway.metering.records import Usage
    from autobuild_json.gateway.metering.units import weighted_usage_micro

    with pytest.raises(ValueError, match="quota_overflow"):
        weighted_usage_micro(Usage(10, 0), 10**37, 0)
