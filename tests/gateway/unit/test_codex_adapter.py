import pytest


def test_refresh_result_classification_never_retries_uncertain_grant():
    from autobuild_json.gateway.accounts.refresh import refresh_state
    assert refresh_state("timeout_after_send") == "refresh_uncertain"
    assert refresh_state("invalid_grant") == "reauth_required"
    assert refresh_state("success") == "active"


@pytest.mark.parametrize("options", [
    {"max_output_tokens": 1},
    {"max_output_tokens": 10000},
    {"temperature": 0, "top_p": 0.9},
    {"max_output_tokens": 10, "temperature": 0.7, "top_p": 1},
    {},
])
def test_codex_normalizes_unsupported_generation_controls_without_mutating_request(options):
    from autobuild_json.gateway.providers.codex import codex_body
    from autobuild_json.gateway.contracts import InferenceRequest, GenerationOptions
    request = InferenceRequest(model="m", options=GenerationOptions(**options, parallel_tool_calls=False))
    before = request.model_dump()
    body = codex_body(request, "actual")
    assert body["stream"] is True and body["store"] is False
    assert body["instructions"] == "" and body["model"] == "actual"
    assert body["parallel_tool_calls"] is False
    assert not set(body) & {"max_output_tokens", "temperature", "top_p"}
    assert request.model_dump() == before


def test_non_codex_responses_keeps_generation_controls():
    from autobuild_json.gateway.contracts import InferenceRequest, GenerationOptions
    from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec
    options = {"max_output_tokens": 10, "temperature": 0.7, "top_p": 0.9}
    request = InferenceRequest(model="m", options=GenerationOptions(**options))
    body = ResponsesCodec().upstream_body(request, "actual")
    assert {name: body[name] for name in options} == options
