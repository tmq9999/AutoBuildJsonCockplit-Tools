"""Offline redaction and nullable-error checks, not live provider evidence."""
import json

import httpx

from tests.gateway.live_key_concurrency import response_summary


def test_completed_response_with_null_error_retains_status_and_marker():
    response = httpx.Response(200, json={
        "error": None,
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": "GATEWAY_CONCURRENCY_OK"}]}],
    })
    assert response_summary(response, "GATEWAY_CONCURRENCY_OK") == {
        "http_status": 200, "code": None, "stage": None, "marker_match": True,
    }


def test_concurrency_rejection_exposes_code_not_private_body():
    response = httpx.Response(429, json={"error": {
        "code": "concurrency_limit", "stage": "quota", "message": "SECRET",
    }})
    result = response_summary(response, "GATEWAY_CONCURRENCY_OK")
    assert result == {
        "http_status": 429, "code": "concurrency_limit", "stage": "quota", "marker_match": False,
    }
    assert "SECRET" not in json.dumps(result)
