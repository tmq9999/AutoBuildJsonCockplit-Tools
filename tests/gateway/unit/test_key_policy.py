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
