"""Failure lifecycles must remain visible to Responses clients without fake success."""
import json

import pytest

from autobuild_json.gateway.contracts import InferenceEvent, Text
from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec


@pytest.mark.parametrize("started", [False, True])
def test_stream_failure_has_failed_response_terminal_even_before_first_upstream_event(started):
    codec = ResponsesCodec(response_id="resp_public", model="m")
    if started:
        codec.encode_event(InferenceEvent(kind="started"))
        codec.encode_event(InferenceEvent(kind="block_started", item_id="m1", block=Text(text="")))
        codec.encode_event(InferenceEvent(kind="text_delta", item_id="m1", delta="partial"))
    wire = b"".join(codec.encode_event(InferenceEvent(kind="error", error_code="upstream_error")))
    values = [json.loads(line[6:]) for line in wire.splitlines() if line.startswith(b"data: ")]
    assert len(values) == 1
    assert values[0]["type"] == "response.failed"
    assert values[0]["response"]["status"] == "failed"
    assert values[0]["response"]["id"] == "resp_public"
    assert values[0]["response"]["error"] == {"code": "upstream_error", "message": "upstream_error"}
    assert values[0]["response"]["usage"] is None
    assert b"response.completed" not in wire


def test_stream_failure_never_exposes_unallowlisted_error_text():
    codec = ResponsesCodec(model="m")
    codec.encode_event(InferenceEvent(kind="started"))
    wire = b"".join(codec.encode_event(InferenceEvent(kind="error", error_code="SECRET-provider-message")))
    assert b"SECRET" not in wire
    assert b"internal_error" in wire


@pytest.mark.parametrize("first_terminal", ["error", "finished"])
def test_stream_never_emits_failure_after_an_existing_terminal(first_terminal):
    codec = ResponsesCodec(model="m")
    codec.encode_event(InferenceEvent(kind="started"))
    event = (InferenceEvent(kind="error", error_code="upstream_error") if first_terminal == "error"
             else InferenceEvent(kind="finished", finish_reason="stop"))
    codec.encode_event(event)
    assert codec.encode_event(InferenceEvent(kind="error", error_code="upstream_error")) == []
