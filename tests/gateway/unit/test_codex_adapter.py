import pytest


def test_refresh_result_classification_never_retries_uncertain_grant():
    from autobuild_json.gateway.accounts.refresh import refresh_state
    assert refresh_state("timeout_after_send") == "refresh_uncertain"
    assert refresh_state("invalid_grant") == "reauth_required"
    assert refresh_state("success") == "active"


def test_codex_does_not_silently_remove_requested_output_cap():
    from autobuild_json.gateway.providers.codex import codex_body
    from autobuild_json.gateway.contracts import InferenceRequest, GenerationOptions
    from autobuild_json.gateway.errors import GatewayError
    with pytest.raises(GatewayError, match="unsupported_feature"):
        codex_body(InferenceRequest(model="m", options=GenerationOptions(max_output_tokens=10)), "actual")
    body = codex_body(InferenceRequest(model="m"), "actual")
    assert body["stream"] is True and body["store"] is False
    assert "max_output_tokens" not in body
