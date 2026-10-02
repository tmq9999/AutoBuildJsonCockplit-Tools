"""Offline checks of the opt-in verifier's diagnostic redaction boundary."""
import json

import pytest

from tests.gateway import live_codex_cli


@pytest.mark.parametrize("message,hint", [
    ("stream disconnected before completion: private data", "stream_disconnected"),
    ("error decoding response body: private data", "response_decode"),
    ("error sending request for url: private data", "request_transport"),
    ("missing field `private-field`", "response_schema"),
    ("operation timed out: private data", "timeout"),
])
def test_client_error_is_classified_without_echoing_message(message, hint):
    raw = json.dumps({"type": "turn.failed", "error": {"message": message}}).encode()
    result = live_codex_cli.client_diagnostics(raw, b"")
    assert result == {"safe_client_errors": [], "failure_hints": [hint],
                      "unclassified_client_error": False}
    assert "private" not in json.dumps(result)


def test_stderr_is_classified_but_never_echoed():
    result = live_codex_cli.client_diagnostics(b"", b"error decoding response body SECRET provider payload")
    assert result == {"safe_client_errors": [], "failure_hints": ["response_decode"],
                      "unclassified_client_error": False}
    assert "SECRET" not in json.dumps(result)


def test_known_gateway_code_survives_without_raw_cli_event():
    raw = json.dumps({"type": "error", "message": "upstream_error SECRET"}).encode()
    assert live_codex_cli.client_diagnostics(raw, b"") == {
        "safe_client_errors": ["upstream_error"], "failure_hints": [],
        "unclassified_client_error": False}


def test_unknown_failure_is_explicit_instead_of_looking_successful():
    raw = json.dumps({"type": "error", "message": "unrecognized SECRET"}).encode()
    assert live_codex_cli.client_diagnostics(raw, b"private stderr") == {
        "safe_client_errors": [], "failure_hints": [], "unclassified_client_error": True}


def test_unknown_stderr_is_explicit_instead_of_looking_successful():
    assert live_codex_cli.client_diagnostics(b"", b"unrecognized SECRET") == {
        "safe_client_errors": [], "failure_hints": [], "unclassified_client_error": True}


def test_successful_agent_text_is_not_treated_as_a_client_error():
    raw = json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": "upstream_error stream disconnected SECRET"}}).encode()
    assert live_codex_cli.client_diagnostics(raw, b"") == {
        "safe_client_errors": [], "failure_hints": [], "unclassified_client_error": False}


@pytest.mark.parametrize("code", ["gateway_busy", "quota_exceeded", "invalid_api_key",
                                 "upstream_unavailable", "unsupported_reasoning_effort"])
def test_cli_diagnostics_retains_admission_auth_and_effort_codes(code):
    raw = json.dumps({"type": "turn.failed", "error": {"message": code + " SECRET"}}).encode()
    assert live_codex_cli.client_diagnostics(raw, b"") == {
        "safe_client_errors": [code], "failure_hints": [], "unclassified_client_error": False}


@pytest.mark.parametrize("message,hint", [
    ("Missing environment variable: PRIVATE_KEY", "missing_key_environment"),
    ("Permission denied (os error 13): SECRET-path", "local_permission"),
    ("Operation not permitted (os error 1): SECRET-path", "local_permission"),
    ("unexpected status 503 Service Unavailable: SECRET-body", "http_503"),
    ("exceeded retry limit, last status: 429 Too Many Requests SECRET-body", "http_429"),
    ("HTTP 401: SECRET-body", "http_401"),
])
def test_cli_diagnostics_classifies_local_and_http_failures_without_raw_data(message, hint):
    raw = json.dumps({"type": "error", "message": message}).encode()
    assert live_codex_cli.client_diagnostics(raw, b"") == {
        "safe_client_errors": [], "failure_hints": [hint], "unclassified_client_error": False}


def test_unrecognized_code_containing_known_code_is_not_misclassified():
    raw = json.dumps({"type": "error", "message": "SECRET_upstream_error_PRIVATE"}).encode()
    assert live_codex_cli.client_diagnostics(raw, b"") == {
        "safe_client_errors": [], "failure_hints": [], "unclassified_client_error": True}


def test_arbitrary_number_in_failure_is_not_an_http_status():
    raw = json.dumps({"type": "error", "message": "unrecognized SECRET 503"}).encode()
    assert live_codex_cli.client_diagnostics(raw, b"") == {
        "safe_client_errors": [], "failure_hints": [], "unclassified_client_error": True}
