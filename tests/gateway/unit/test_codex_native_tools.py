"""Native Codex tool lifecycle regressions; synthetic fixtures, not live proof."""
import copy
import json
from types import SimpleNamespace

import pytest

from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec
from autobuild_json.gateway.providers.codex import codex_body
from autobuild_json.gateway.providers.preflight import validate_request
from autobuild_json.gateway.providers.responses_events import ResponsesEvents


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["SECRET-provider-event", "response.reasoning_text.delta"])
async def test_responses_failure_diagnostics_never_log_arbitrary_upstream_values(caplog, kind):
    from autobuild_json.gateway.providers.responses_events import responses_events
    from .test_responses_reasoning import SyntheticResponse, reasoning_wire

    values = [{"type": "response.created", "response": {"id": "SECRET-id"}},
              {"type": kind, "delta": "SECRET-content"}]
    with pytest.raises(GatewayError, match="unsupported_feature"):
        _ = [event async for event in responses_events(SyntheticResponse(reasoning_wire(values)))]
    assert "SECRET" not in caplog.text
    assert "code=unsupported_feature" in caplog.text
    assert ("event_type=other" if kind.startswith("SECRET") else "event_type="+kind) in caplog.text


def custom_events():
    item = {"id": "ctc_1", "type": "custom_tool_call", "call_id": "call_1",
            "name": "exec", "namespace": "functions", "metadata": {}, "input": "", "status": "in_progress",
            "internal_chat_message_metadata_passthrough": None}
    final = dict(item, input='text("probe");', status="completed")
    return [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.custom_tool_call_input.delta", "item_id": "ctc_1", "output_index": 0, "delta": 'text("'},
        {"type": "response.custom_tool_call_input.delta", "item_id": "ctc_1", "output_index": 0, "delta": 'probe");'},
        {"type": "response.custom_tool_call_input.done", "item_id": "ctc_1", "output_index": 0, "input": 'text("probe");'},
        {"type": "response.output_item.done", "output_index": 0, "item": final},
        {"type": "response.completed", "response": {"output": [final],
         "usage": {"input_tokens": 40, "output_tokens": 15,
                   "input_tokens_details": {"cached_tokens": 20},
                   "output_tokens_details": {"reasoning_tokens": 5}}}},
    ]


@pytest.mark.asyncio
async def test_invalid_native_item_logs_local_boundary_without_private_fields(caplog):
    from autobuild_json.gateway.providers.responses_events import responses_events
    from .test_responses_reasoning import SyntheticResponse, reasoning_wire

    values = custom_events()[:2]
    values[1]["item"]["SECRET-field"] = "SECRET-value"
    with pytest.raises(GatewayError, match="unsupported_feature"):
        _ = [event async for event in responses_events(SyntheticResponse(reasoning_wire(values)))]
    assert "SECRET" not in caplog.text
    assert "event_type=response.output_item.added" in caplog.text
    assert "item_type=custom_tool_call" in caplog.text
    assert "origin=native_tool_item" in caplog.text


@pytest.mark.asyncio
async def test_reasoning_schema_diagnostic_uses_fixed_field_names_only(caplog):
    from autobuild_json.gateway.providers.responses_events import responses_events
    from .test_responses_reasoning import SyntheticResponse, reasoning_wire

    values = [
        {"type": "response.created", "response": {"id": "SECRET"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"id": "SECRET", "type": "reasoning", "summary": [],
                  "metadata": {"SECRET": "SECRET"}, "SECRET-field": "SECRET"}},
    ]
    with pytest.raises(GatewayError, match="unsupported_feature"):
        _ = [event async for event in responses_events(SyntheticResponse(reasoning_wire(values)))]
    assert "SECRET" not in caplog.text
    assert "fields=id,metadata,summary,type" in caplog.text
    assert "other_fields=1" in caplog.text


@pytest.mark.asyncio
async def test_upstream_error_event_preserves_only_a_safe_code():
    from autobuild_json.gateway.providers.responses_events import responses_events
    from .test_responses_reasoning import SyntheticResponse, reasoning_wire

    values = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "error", "code": "unsupported_feature", "message": "SECRET"},
    ]
    with pytest.raises(GatewayError, match="unsupported_feature"):
        _ = [event async for event in responses_events(SyntheticResponse(reasoning_wire(values)))]


@pytest.mark.asyncio
async def test_unknown_upstream_error_event_is_redacted_to_upstream_error():
    from autobuild_json.gateway.providers.responses_events import responses_events
    from .test_responses_reasoning import SyntheticResponse, reasoning_wire

    values = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "error", "code": "SECRET-provider-code", "message": "SECRET"},
    ]
    with pytest.raises(GatewayError, match="upstream_error"):
        _ = [event async for event in responses_events(SyntheticResponse(reasoning_wire(values)))]


@pytest.mark.asyncio
@pytest.mark.parametrize("envelope", ["flat", "nested", "failed"])
@pytest.mark.parametrize("code,expected", [
    ("unsupported_feature", "unsupported_feature"),
    ("rate_limited", "rate_limited"),
    ("SECRET-provider-code", "upstream_error"),
    (["SECRET"], "upstream_error"),
])
async def test_prestart_upstream_error_retains_safe_code_without_enabling_retry(caplog, envelope, code, expected):
    from autobuild_json.gateway.providers.responses_events import responses_events
    from .test_responses_reasoning import SyntheticResponse, reasoning_wire

    error = {"code": code, "message": "SECRET-provider-body"}
    value = {"type": "error", **error} if envelope == "flat" else (
        {"type": "error", "error": error} if envelope == "nested" else
        {"type": "response.failed", "response": {"id": "SECRET", "error": error}})
    with pytest.raises(GatewayError, match="^" + expected + "$") as caught:
        _ = [event async for event in responses_events(SyntheticResponse(reasoning_wire([value])))]
    assert caught.value.stage == "stream"
    assert caught.value.status == 502
    assert not getattr(caught.value, "safe_retry", False)
    assert "SECRET" not in caplog.text


def test_custom_tool_stream_preserves_raw_input_namespace_usage_and_replay():
    parser, codec = ResponsesEvents(), ResponsesCodec(model="m")
    frames = []
    for value in custom_events():
        for event in parser.feed(value):
            frames.extend(codec.encode_event(event))
    records = [json.loads(line[6:]) for frame in frames for line in frame.decode().splitlines()
               if line.startswith("data: ")]
    added = next(v["item"] for v in records if v["type"] == "response.output_item.added")
    assert added == {"id": "ctc_1", "type": "custom_tool_call", "call_id": "call_1",
                     "name": "exec", "namespace": "functions", "metadata": {}, "input": "", "status": "in_progress",
                     "internal_chat_message_metadata_passthrough": None}
    assert [v["delta"] for v in records if v["type"] == "response.custom_tool_call_input.delta"] == ['text("', 'probe");']
    final = records[-1]["response"]
    assert final["output"][0] == {**added, "status": "completed", "input": 'text("probe");'}
    assert final["usage"]["input_tokens"] == 40
    assert final["usage"]["input_tokens_details"]["cached_tokens"] == 20
    assert final["usage"]["output_tokens_details"]["reasoning_tokens"] == 5
    assert parser.collector.result().finish_reason == "tool_calls"
    # Tool inputs are grammar text, not JSON function arguments.
    request = codec.decode({"model": "m", "input": final["output"] + [
        {"type": "custom_tool_call_output", "call_id": "call_1", "output": "probe"}]}, {})
    wire = codex_body(request, "m")
    assert wire["input"][0]["input"] == 'text("probe");'
    assert wire["input"][0]["namespace"] == "functions"
    assert wire["input"][1] == {"type": "custom_tool_call_output", "call_id": "call_1", "output": "probe"}
    assert "tools" in request.required_capabilities
    with pytest.raises(ValueError, match="unsupported_feature"):
        request.validate_provider("anthropic")


def test_custom_tool_final_metadata_can_be_filled_without_changing_the_call():
    values = custom_events()
    values[5]["item"]["metadata"] = {"fixture": "finalized"}
    values[5]["item"]["internal_chat_message_metadata_passthrough"] = {"fixture": "finalized"}
    values[-1]["response"]["output"][0] = copy.deepcopy(values[5]["item"])
    parser = ResponsesEvents()
    for value in values:
        parser.feed(value)
    assert parser.collector.result().blocks[0].payload["metadata"] == {"fixture": "finalized"}


@pytest.mark.parametrize("mutation", ["wrong_call", "wrong_namespace", "wrong_input", "wrong_done", "wrong_delta", "oversize"])
def test_custom_tool_rejects_conflicting_or_oversized_lifecycle(mutation):
    values = copy.deepcopy(custom_events())
    if mutation == "wrong_call":
        values[5]["item"]["call_id"] = "other"
    elif mutation == "wrong_namespace":
        values[5]["item"]["namespace"] = "other"
    elif mutation == "wrong_input":
        values[5]["item"]["input"] = "different"
    elif mutation == "wrong_done":
        values[4]["input"] = "different"
    elif mutation == "wrong_delta":
        values[2]["type"] = "response.function_call_arguments.delta"
    else:
        values[2]["delta"] = "x" * 1_048_577
    parser = ResponsesEvents()
    with pytest.raises((ValueError, GatewayError)):
        for value in values:
            parser.feed(value)


@pytest.mark.parametrize("kind, payload", [
    ("function_call", {"name": "exec_command", "arguments": "{}", "namespace": "functions"}),
    ("custom_tool_call_output", {"output": [{"type": "input_text", "text": "probe"}]}),
])
def test_codex_native_replay_preserves_namespaces_and_structured_tool_output(kind, payload):
    item = {"type": kind, "call_id": "call_1", **payload}
    request = ResponsesCodec().decode({"model": "m", "input": [item]}, {})
    assert codex_body(request, "m")["input"] == [item]


@pytest.mark.parametrize("extra", [
    {"client_metadata": {"session_id": "fixture"}}, {"service_tier": "priority"},
])
@pytest.mark.parametrize("adapter,wire_api", [("anthropic", "messages"), ("gemini", "generateContent"),
    ("ollama", "chat"), ("openai_compatible", "chat")])
def test_request_hints_are_not_silently_dropped_on_other_protocols(extra, adapter, wire_api):
    request = ResponsesCodec().decode({"model": "m", "input": "hi", **extra}, {})
    with pytest.raises(GatewayError, match="unsupported_feature"):
        validate_request(request, SimpleNamespace(adapter=adapter, wire_api=wire_api, upstream_model="m"))


def test_additional_tools_require_tool_capability_and_detach_nested_client_data():
    item = {"type": "additional_tools", "role": "developer", "tools": [
        {"type": "namespace", "name": "functions", "tools": [
            {"type": "custom", "name": "exec", "format": {"type": "text"}}]}]}
    request = ResponsesCodec().decode({"model": "m", "input": [item]}, {})
    assert "tools" in request.required_capabilities
    item["tools"][0]["tools"][0]["name"] = "mutated"
    assert codex_body(request, "m")["input"][0]["tools"][0]["tools"][0]["name"] == "exec"


@pytest.mark.parametrize("phase", ["commentary", "final_answer"])
def test_assistant_message_phase_survives_stream_and_followup_request(phase):
    parser, codec = ResponsesEvents(), ResponsesCodec(model="m")
    frames = []
    item = {"id": "msg_1", "type": "message", "role": "assistant", "phase": phase,
            "status": "completed", "content": [{"type": "output_text", "text": "probe", "annotations": []}]}
    values = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "output_index": 0, "item": dict(item, content=[], status="in_progress")},
        {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "delta": "probe"},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {"output": [item]}},
    ]
    for value in values:
        for event in parser.feed(value):
            frames.extend(codec.encode_event(event))
    records = [json.loads(line[6:]) for frame in frames for line in frame.decode().splitlines()
               if line.startswith("data: ")]
    assert next(v["item"] for v in records if v["type"] == "response.output_item.added")["phase"] == phase
    output = records[-1]["response"]["output"]
    assert output[0]["phase"] == phase
    request = codec.decode({"model": "m", "input": output}, {})
    assert codex_body(request, "m")["input"][0]["phase"] == phase


@pytest.mark.parametrize("role,phase", [("user", "commentary"), ("assistant", "unknown")])
def test_invalid_message_phase_is_not_silently_dropped(role, phase):
    with pytest.raises(GatewayError, match="invalid_request"):
        ResponsesCodec().decode({"model": "m", "input": [
            {"type": "message", "role": role, "phase": phase, "content": "probe"}]}, {})


def test_message_phase_mismatch_between_added_and_done_is_rejected():
    parser = ResponsesEvents()
    item = {"id": "msg_1", "type": "message", "role": "assistant", "phase": "commentary",
            "status": "completed", "content": [{"type": "output_text", "text": "probe", "annotations": []}]}
    values = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": dict(item, content=[], status="in_progress")},
        {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "delta": "probe"},
        {"type": "response.output_item.done", "output_index": 0,
         "item": dict(item, phase="final_answer")},
    ]
    with pytest.raises(ValueError, match="message_phase_mismatch"):
        for value in values:
            parser.feed(value)


def test_message_phase_mismatch_in_terminal_output_is_rejected():
    parser = ResponsesEvents()
    item = {"id": "msg_1", "type": "message", "role": "assistant", "phase": "commentary",
            "status": "completed", "content": [{"type": "output_text", "text": "probe", "annotations": []}]}
    values = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": dict(item, content=[], status="in_progress")},
        {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "delta": "probe"},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {"output": [dict(item, phase="final_answer")]}},
    ]
    with pytest.raises(ValueError, match="message_phase_mismatch"):
        for value in values:
            parser.feed(value)


@pytest.mark.parametrize("part", [
    {"type": "input_image", "image_url": "data:image/png;base64,AA==", "detail": "low"},
    {"type": "input_file", "filename": "report.txt", "file_data": "data:text/plain;base64,QQ=="},
])
def test_custom_tool_output_accepts_official_media_parts(part):
    item = {"type": "custom_tool_call_output", "call_id": "call_1", "output": [part],
            "async": False, "caller": {"type": "program", "caller_id": "call_1"}}
    request = ResponsesCodec().decode({"model": "m", "input": [item]}, {})
    assert codex_body(request, "m")["input"] == [item]


@pytest.mark.parametrize("kind,field", [("custom_tool_call", "input"), ("function_call", "arguments")])
def test_native_tool_calls_accept_official_caller_and_async_fields(kind, field):
    item = {"type": kind, "call_id": "call_1", "name": "exec", field: "probe", "async": True,
            "caller": {"type": "program", "caller_id": "program_1"}}
    request = ResponsesCodec().decode({"model": "m", "input": [item]}, {})
    assert codex_body(request, "m")["input"] == [item]


@pytest.mark.parametrize("adapter,wire_api", [("anthropic", "messages"), ("gemini", "generateContent"),
    ("ollama", "chat"), ("openai_compatible", "chat")])
def test_phase_is_not_silently_dropped_on_non_responses_routes(adapter, wire_api):
    request = ResponsesCodec().decode({"model": "m", "input": [
        {"role": "assistant", "phase": "commentary", "content": "Checking"}]}, {})
    with pytest.raises(GatewayError, match="unsupported_feature"):
        validate_request(request, SimpleNamespace(adapter=adapter, wire_api=wire_api, upstream_model="m"))


def test_standard_function_with_id_and_status_remains_portable():
    request = ResponsesCodec().decode({"model": "m", "input": [
        {"type": "function_call", "id": "fc_1", "status": "completed", "call_id": "call_1",
         "name": "lookup", "arguments": "{}"}]}, {})
    validate_request(request, SimpleNamespace(adapter="openai_compatible", wire_api="responses", upstream_model="m"))


def test_native_function_stream_preserves_caller_without_namespace():
    parser, codec = ResponsesEvents(), ResponsesCodec(model="m")
    item = {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup",
            "arguments": "", "async": False, "caller": {"type": "direct"}, "status": "in_progress"}
    final = dict(item, arguments="{}", status="completed")
    values = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 0, "delta": "{}"},
        {"type": "response.output_item.done", "output_index": 0, "item": final},
        {"type": "response.completed", "response": {"output": [final]}},
    ]
    for value in values:
        for event in parser.feed(value):
            codec.encode_event(event)
    assert codec.encode_result(codec.collector.result())["output"] == [final]


def test_phase_can_arrive_at_item_done_and_survives_encoding():
    parser, codec = ResponsesEvents(), ResponsesCodec(model="m")
    item = {"id": "msg_1", "type": "message", "role": "assistant", "content": []}
    final = dict(item, phase="final_answer", content=[{"type": "output_text", "text": "probe"}])
    values = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "delta": "probe"},
        {"type": "response.output_item.done", "output_index": 0, "item": final},
        {"type": "response.completed", "response": {"output": [final]}},
    ]
    for value in values:
        for event in parser.feed(value):
            codec.encode_event(event)
    assert codec.encode_result(codec.collector.result())["output"][0]["phase"] == "final_answer"


@pytest.mark.parametrize("part", [
    {"type": "input_image", "image_url": "https://example.com/image.png", "file_id": 42},
    {"type": "input_file", "file_id": "file_1", "file_data": {}},
    {"type": "input_file", "file_id": "x" * 513},
])
def test_custom_tool_media_fields_are_typed_and_bounded(part):
    with pytest.raises(GatewayError, match="invalid_request"):
        ResponsesCodec().decode({"model": "m", "input": [
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": [part]}]}, {})


def test_custom_tool_images_require_vision_capability():
    request = ResponsesCodec().decode({"model": "m", "input": [
        {"type": "custom_tool_call_output", "call_id": "call_1", "output": [
            {"type": "input_image", "image_url": "https://example.com/image.png"}]}]}, {})
    assert "vision" in request.required_capabilities
