"""Offline HTTP diagnostics, not live request compatibility evidence."""
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.http.app import create_gateway_app
from autobuild_json.gateway.http.responses_diagnostics import log_responses_rejection


def application():
    async def authenticate(*args):
        return object()

    async def never_dispatch(*args, **kwargs):
        pytest.fail("Invalid request must not dispatch")

    return create_gateway_app(SimpleNamespace(authenticate=authenticate), object(),
        SimpleNamespace(prepare=never_dispatch, session_digest=never_dispatch, meta=never_dispatch),
        allowed_hosts={"test"})


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/responses", "/backend-api/codex/responses",
                                  "/v1/responses/compact", "/backend-api/codex/responses/compact"])
async def test_native_item_validation_records_safe_reason_not_request_body(caplog, path):
    body = {"model": "PRIVATE-model", "input": [{"type": "custom_tool_call",
        "call_id": "PRIVATE-call", "name": "PRIVATE-tool", "input": "PRIVATE-prompt", "namespace": None}]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()), base_url="http://test") as client:
        response = await client.post(path, headers={"Authorization": "Bearer PRIVATE-key"}, json=body)
    assert response.status_code == 400
    assert response.json() == {"error": {"code": "invalid_request", "message": "invalid_request", "stage": "request"}}
    assert "responses request rejected code=invalid_request" in caplog.text
    assert "origin=openai_responses.native_tool_item" in caplog.text
    assert "reason=invalid_tool_namespace" in caplog.text
    assert "PRIVATE" not in caplog.text and "PRIVATE" not in response.text


@pytest.mark.asyncio
async def test_pydantic_validation_records_only_allowlisted_locations_and_types(caplog):
    body = {"model": "PRIVATE-model", "input": "PRIVATE-prompt",
            "client_metadata": {"PRIVATE-field": {"PRIVATE-key": "PRIVATE-value"}}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()), base_url="http://test") as client:
        response = await client.post("/v1/responses", headers={"Authorization": "Bearer PRIVATE-key"}, json=body)
    assert response.status_code == 400
    assert "reason=validation_error" in caplog.text
    assert "validation=client_metadata.other:string_type" in caplog.text
    assert "PRIVATE" not in caplog.text


@pytest.mark.asyncio
async def test_invalid_header_is_distinguished_from_codec_validation(caplog):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()), base_url="http://test") as client:
        response = await client.post("/v1/responses", headers={"Authorization": "Bearer PRIVATE-key",
            "X-Gateway-Session": "PRIVATE-" * 30}, json={"model": "m", "input": "hello"})
    assert response.status_code == 400
    assert "origin=auth.gateway_session" in caplog.text
    assert "PRIVATE" not in caplog.text


@pytest.mark.asyncio
async def test_invalid_json_and_unsupported_fields_never_log_raw_body(caplog):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()), base_url="http://test") as client:
        response = await client.post("/v1/responses", headers={"Authorization": "Bearer PRIVATE-key",
            "Content-Type": "application/json"}, content='{"PRIVATE-body":')
        assert response.status_code == 400
        assert "reason=invalid_json" in caplog.text
        caplog.clear()
        response = await client.post("/v1/responses", headers={"Authorization": "Bearer PRIVATE-key"},
                                     json={"model": "m", "input": "hello", "PRIVATE-field": "PRIVATE-value"})
    assert response.status_code == 400
    assert "code=unsupported_feature" in caplog.text
    assert "PRIVATE" not in caplog.text
    assert json.loads(response.content)["error"]["code"] == "unsupported_feature"


def test_diagnostics_bound_validation_and_redact_custom_error_messages(caplog):
    validation_error = ValidationError.from_exception_data("PRIVATE-model", [
        {"type": PydanticCustomError("PRIVATE-error", "PRIVATE-message {secret}", {"secret": "PRIVATE-token"}),
         "loc": ("messages", index, "PRIVATE-key", "content", "text", "PRIVATE-key", "PRIVATE-deep"),
         "input": "PRIVATE-value"} for index in range(20)])
    error = GatewayError("invalid_request")
    error.__context__ = validation_error
    log_responses_rejection("/v1/responses", error)
    assert "reason=validation_error" in caplog.text
    assert caplog.text.count("messages.*.other.content.text.other:other") == 6
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("cause,reason", [(ValueError("PRIVATE-token"), "invalid_value"),
    (KeyError("PRIVATE-key"), "missing_field"), (TypeError("PRIVATE-body"), "invalid_type"),
    (AttributeError("PRIVATE-field"), "invalid_shape")])
def test_diagnostics_never_format_unknown_exceptions_or_cycle_forever(caplog, cause, reason):
    error = GatewayError("invalid_request")
    error.__context__ = cause
    cause.__context__ = error
    log_responses_rejection("/v1/responses", error)
    assert f"reason={reason}" in caplog.text
    assert "origin=other" in caplog.text
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("path,code", [("/v1/chat/completions", "invalid_request"),
    ("/v1/responses/PRIVATE-path", "invalid_request"), ("/v1/responses", "invalid_api_key")])
def test_diagnostics_are_limited_to_responses_validation_failures(caplog, path, code):
    log_responses_rejection(path, GatewayError(code))
    assert not caplog.records
