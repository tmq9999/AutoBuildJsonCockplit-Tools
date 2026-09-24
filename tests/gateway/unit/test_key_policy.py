import pytest
from pydantic import ValidationError


@pytest.mark.parametrize("values", [
    {"total_micro": -1}, {"day_micro": True}, {"rpm": 0}, {"concurrency": 0},
    {"protocols": {"arbitrary"}}, {"total_micro": 10 ** 38}, {"month_micro": "1"},
    {"expires_at": "2026-09-24T00:00:00"}, {"model_ids": {""}},
])
def test_policy_rejects_ambiguous_or_unsafe_values(values):
    from autobuild_json.gateway.identity.policy import KeyPolicy
    with pytest.raises(ValidationError):
        KeyPolicy(**values)


def test_policy_default_is_denied_and_null_is_explicit_unlimited():
    from autobuild_json.gateway.identity.policy import KeyPolicy
    policy = KeyPolicy()
    assert policy.total_micro == 0 and policy.model_ids == frozenset()
    assert KeyPolicy(total_micro=None).total_micro is None


def test_policy_is_immutable_snapshot():
    from autobuild_json.gateway.identity.policy import KeyPolicy
    policy = KeyPolicy(model_ids={"model"})
    with pytest.raises(ValidationError):
        policy.total_micro = 100
    assert not hasattr(policy.model_ids, "add")


def test_coefficient_overrides_are_unique_nonnegative_and_frozen():
    from autobuild_json.gateway.identity.policy import KeyPolicy, ModelRate
    rate = ModelRate(model_id="m", input_micro=1_000_000, output_micro=3_000_000)
    policy = KeyPolicy(model_overrides=(rate,))
    assert policy.model_overrides[0].output_micro == 3_000_000
    with pytest.raises(ValidationError):
        KeyPolicy(model_overrides=(rate, rate))
    with pytest.raises(ValidationError):
        ModelRate(model_id="m", input_micro=-1, output_micro=1)
    with pytest.raises(ValidationError):
        rate.output_micro = 0


def test_model_cache_rates_serialize_in_policy_json_without_changing_legacy_rates():
    from autobuild_json.gateway.identity.policy import KeyPolicy, ModelRate
    from autobuild_json.gateway.routing.records import ModelConfig

    legacy = ModelRate(model_id="m", input_micro=3, output_micro=7)
    assert (legacy.cache_read_micro, legacy.cache_write_micro) == (None, None)
    policy = KeyPolicy(model_overrides=(legacy,))
    assert policy.model_dump(mode="json")["model_overrides"][0] == {
        "model_id": "m", "input_micro": 3, "output_micro": 7,
        "cache_read_micro": None, "cache_write_micro": None,
    }
    model = ModelConfig(model_id="m", identity="m")
    assert (model.input_micro, model.output_micro, model.cache_read_micro,
            model.cache_write_micro) == (1_000_000, 1_000_000, None, None)


@pytest.mark.parametrize("rate", [-1, True, 1.5, "2", 10**38])
def test_model_and_policy_cache_rates_use_strict_quota(rate):
    from autobuild_json.gateway.identity.policy import ModelRate
    from autobuild_json.gateway.routing.records import ModelConfig

    with pytest.raises(ValidationError):
        ModelRate(model_id="m", input_micro=1, output_micro=1, cache_read_micro=rate)
    with pytest.raises(ValidationError):
        ModelConfig(model_id="m", identity="m", cache_write_micro=rate)


def test_route_snapshot_validates_only_new_cache_rates():
    from uuid import uuid4

    from autobuild_json.gateway.metering.records import Bounds
    from autobuild_json.gateway.routing.records import RouteSnapshot

    ids = [uuid4() for _ in range(3)]
    args = (*ids, "m", "upstream", "openai_compatible", "https://example.invalid", "bearer",
            1, frozenset({"text"}), Bounds(10, 2), None, None, 0, 3, 7, 30)
    legacy = RouteSnapshot(*args)
    assert (legacy.cache_read_micro, legacy.cache_write_micro) == (None, None)
    assert RouteSnapshot(*args, cache_read_micro=0, cache_write_micro=10**38 - 1).cache_read_micro == 0
    for bad_rate in (-1, True, 1.5, float("nan"), 10**38):
        with pytest.raises(ValueError, match="invalid_usage"):
            RouteSnapshot(*args, cache_read_micro=bad_rate)
        with pytest.raises(ValueError, match="invalid_usage"):
            RouteSnapshot(*args, cache_write_micro=bad_rate)
