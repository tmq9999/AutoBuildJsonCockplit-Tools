import copy
import json

import pytest

from autobuild_json.gateway.contracts import Reasoning, REASONING_BYTE_LIMIT
from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.protocols.common import EventCollector
from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec
from autobuild_json.gateway.providers.responses_events import responses_events


def reasoning_events(*, summary_only_at_done=False):
    reasoning = {"id": "rs_private", "type": "reasoning", "summary": [
        {"type": "summary_text", "text": "Published summary."},
        {"type": "summary_text", "text": "Another published summary."}], "encrypted_content": "opaque-replay-payload"}
    message = {"id": "msg1", "type": "message", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": "Answer", "annotations": []}]}
    values = [
        {"type": "response.created", "response": {"id": "upstream-private", "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"id": "rs_private", "type": "reasoning", "summary": []}},
    ]
    if not summary_only_at_done:
        for index, part in enumerate(reasoning["summary"]):
            identity = {"item_id": "rs_private", "output_index": 0, "summary_index": index}
            values.extend([
                dict(type="response.reasoning_summary_part.added", **identity, part={"type": "summary_text", "text": ""}),
                dict(type="response.reasoning_summary_text.delta", **identity, delta=part["text"]),
                dict(type="response.reasoning_summary_text.done", **identity, text=part["text"]),
                dict(type="response.reasoning_summary_part.done", **identity, part=part),
            ])
    values.extend([
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
        {"type": "response.output_item.added", "output_index": 1, "item": dict(message, content=[])},
        {"type": "response.output_text.delta", "item_id": "msg1", "output_index": 1, "content_index": 0, "delta": "Answer"},
        {"type": "response.output_item.done", "output_index": 1, "item": message},
        {"type": "response.completed", "response": {"id": "upstream-private", "status": "completed",
         "output": [reasoning, message], "usage": {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9,
         "input_tokens_details": {"cached_tokens": 3}, "output_tokens_details": {"reasoning_tokens": 2}}}},
    ])
    return values


def reasoning_wire(values=None):
    return b"".join(("event: "+value["type"]+"\ndata: "+json.dumps(value)+"\n\n").encode()
                    for value in (reasoning_events() if values is None else values))


class SyntheticResponse:
    def __init__(self, wire):
        self.wire = wire

    async def chunks(self):
        # Exercise arbitrary HTTP boundaries across summary/encrypted fields.
        for start in range(0, len(self.wire), 37):
            yield self.wire[start:start+37]


async def canonical(values=None):
    return [event async for event in responses_events(SyntheticResponse(reasoning_wire(values)))]


@pytest.mark.asyncio
async def test_codex_empty_terminal_retains_streamed_text_reasoning_and_exact_usage():
    values = reasoning_events()
    expected = collected(await canonical(values))
    values[-1]["response"]["output"] = []
    events = [e async for e in responses_events(SyntheticResponse(reasoning_wire(values)),
                                               allow_empty_terminal_output=True)]
    result = collected(events)
    assert result.blocks == expected.blocks
    assert result.usage == expected.usage
    assert result.usage.cached_read == 3 and result.usage.output_tokens == 5
    # This relaxation belongs only to Codex, not every Responses provider.
    with pytest.raises(GatewayError):
        await canonical(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["unclosed", "missing_terminal", "mismatched", "not_list"])
async def test_codex_empty_terminal_does_not_accept_incomplete_or_conflicting_lifecycle(mutation):
    values = reasoning_events()
    values[-1]["response"]["output"] = []
    if mutation == "unclosed":
        values.pop(-2)
    elif mutation == "missing_terminal":
        values.pop()
    elif mutation == "mismatched":
        values[-1]["response"]["output"] = [{"id": "unknown"}]
    else:
        values[-1]["response"]["output"] = None
    with pytest.raises(GatewayError):
        _ = [e async for e in responses_events(SyntheticResponse(reasoning_wire(values)),
                                               allow_empty_terminal_output=True)]


def collected(events):
    collector = EventCollector("resp_public", "model")
    for event in events:
        collector.feed(event)
    return collector.result()


@pytest.mark.asyncio
@pytest.mark.parametrize("at_done", [False, True])
async def test_reasoning_summaries_encrypted_replay_and_usage_survive_native_responses(at_done):
    values = reasoning_events(summary_only_at_done=at_done)
    events = await canonical(values)
    result = collected(events)
    block = result.blocks[0]
    assert isinstance(block, Reasoning)
    assert block.summary == ("Published summary.", "Another published summary.")
    assert block.encrypted_content == "opaque-replay-payload"
    assert "opaque-replay-payload" not in repr(block)
    assert "Published summary." not in repr(block)
    codec = ResponsesCodec(model="model")
    output = codec.encode_result(result)["output"]
    assert output[0] == values[-1]["response"]["output"][0]
    request = codec.decode({"model": "model", "input": output}, {})
    assert codec.upstream_body(request, "upstream")["input"][0] == output[0]
    assert result.usage.reasoning == 2 and result.usage.cached_read == 3
    wire = b"".join(frame for event in events for frame in codec.encode_event(event))
    roundtrip = [event async for event in responses_events(SyntheticResponse(wire))]
    assert collected(roundtrip).blocks == result.blocks
    if not at_done:
        assert b"event: response.reasoning_summary_part.added" in wire
        assert b"event: response.reasoning_summary_text.delta" in wire
        assert b"event: response.reasoning_summary_text.done" in wire
        assert b"event: response.reasoning_summary_part.done" in wire


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [None, {}, {"turn": "PRIVATE-metadata"}])
async def test_codex_reasoning_metadata_fields_are_accepted_and_not_replayed(metadata, caplog):
    values = copy.deepcopy(reasoning_events())
    for index in (1, 10):
        values[index]["item"].update(content=[], encrypted_content="opaque-replay-payload",
                                    metadata=metadata, internal_chat_message_metadata_passthrough=None)
    values[-1]["response"]["output"][0] = dict(values[10]["item"], metadata={"turn": "PRIVATE-final"},
        internal_chat_message_metadata_passthrough={"trace": "PRIVATE-internal"})
    events = await canonical(values)
    result = collected(events)
    assert result.blocks[0].summary == ("Published summary.", "Another published summary.")
    assert result.blocks[0].encrypted_content == "opaque-replay-payload"
    encoded = ResponsesCodec(model="model").encode_result(result)
    assert "metadata" not in encoded["output"][0]
    assert "internal_chat_message_metadata_passthrough" not in encoded["output"][0]
    codec = ResponsesCodec(model="model")
    wire = b"".join(frame for event in events for frame in codec.encode_event(event))
    assert b"event: response.completed" in wire and b"event: response.failed" not in wire
    assert b"PRIVATE" not in wire and "PRIVATE" not in caplog.text
    request = codec.decode({"model": "model", "input": values[-1]["response"]["output"]}, {})
    assert codec.upstream_body(request, "upstream")["input"][0] == encoded["output"][0]
    assert result.usage.input_tokens == 4 and result.usage.output_tokens == 5


@pytest.mark.parametrize("field", ["metadata", "internal_chat_message_metadata_passthrough"])
def test_reasoning_transport_metadata_is_bounded_json(field):
    from autobuild_json.gateway.protocols.openai_responses import reasoning_item

    item = {"type": "reasoning", "id": "rs1", "summary": []}
    with pytest.raises(ValueError, match="reasoning_metadata_limit_exceeded"):
        reasoning_item(dict(item, **{field: {"data": "x" * (256 * 1024)}}))
    with pytest.raises(ValueError, match="invalid_reasoning_metadata"):
        reasoning_item(dict(item, **{field: float("nan")}))


def test_reasoning_metadata_does_not_allow_unknown_fields_or_raw_reasoning():
    from autobuild_json.gateway.protocols.openai_responses import reasoning_item

    item = {"type": "reasoning", "id": "rs1", "summary": [], "metadata": {},
            "internal_chat_message_metadata_passthrough": None}
    with pytest.raises(GatewayError, match="unsupported_feature"):
        reasoning_item(dict(item, unknown_field="PRIVATE"))
    with pytest.raises(ValueError, match="invalid_reasoning_content"):
        reasoning_item(dict(item, content=[{"type": "reasoning_text", "text": "PRIVATE"}]))


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "anthropic", "gemini", "ollama", "ollama_generate"])
async def test_other_protocols_omit_reasoning_without_leaking_or_losing_usage(protocol):
    from autobuild_json.gateway.protocols.openai_chat import OpenAIChatCodec
    from autobuild_json.gateway.protocols.anthropic import AnthropicCodec
    from autobuild_json.gateway.protocols.gemini import GeminiCodec
    from autobuild_json.gateway.protocols.ollama import OllamaCodec
    codec = {"chat": OpenAIChatCodec, "anthropic": AnthropicCodec, "gemini": GeminiCodec,
             "ollama": OllamaCodec, "ollama_generate": lambda: OllamaCodec(generate=True)}[protocol]()
    events = await canonical()
    encoded = json.dumps(codec.encode_result(collected(events)))
    wire = b"".join(frame for event in events for frame in codec.encode_event(event)).decode()
    for body in (encoded, wire):
        assert "Answer" in body
        assert "opaque-replay-payload" not in body and "Published summary." not in body and "rs_private" not in body
    if protocol == "anthropic":
        records = [json.loads(line[6:]) for line in wire.splitlines() if line.startswith("data: ")]
        assert [item["index"] for item in records if item["type"] == "content_block_start"] == [0]
        assert [item["index"] for item in records if item["type"] == "content_block_stop"] == [0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["duplicate_id", "duplicate_index", "unknown_part", "bad_output_index", "duplicate_part",
    "part_gap", "duplicate_text_done", "delta_after_done", "duplicate_part_done", "part_done_before_text_done",
    "unclosed_part", "unknown_item_done", "duplicate_item_done", "unclosed_item", "summary_mismatch",
    "terminal_unknown", "terminal_summary_mismatch", "after_terminal", "raw_content"])
async def test_reasoning_rejects_invalid_lifecycle(mutation):
    values = copy.deepcopy(reasoning_events())
    if mutation == "duplicate_id":
        values.insert(2, dict(values[1], output_index=3))
    elif mutation == "duplicate_index":
        values.insert(2, dict(values[1], item=dict(values[1]["item"], id="different")))
    elif mutation == "unknown_part":
        values[3]["item_id"] = "unknown"
    elif mutation == "bad_output_index":
        values[3]["output_index"] = 5
    elif mutation == "duplicate_part":
        values.insert(3, copy.deepcopy(values[2]))
    elif mutation == "part_gap":
        values[2]["summary_index"] = 1
    elif mutation == "duplicate_text_done":
        values.insert(5, copy.deepcopy(values[4]))
    elif mutation == "delta_after_done":
        values.insert(5, copy.deepcopy(values[3]))
    elif mutation == "duplicate_part_done":
        values.insert(6, copy.deepcopy(values[5]))
    elif mutation == "part_done_before_text_done":
        del values[4]
    elif mutation == "unclosed_part":
        del values[9]
    elif mutation == "unknown_item_done":
        values[10]["item"]["id"] = "unknown"
    elif mutation == "duplicate_item_done":
        values.insert(11, copy.deepcopy(values[10]))
    elif mutation == "unclosed_item":
        del values[10]
    elif mutation == "summary_mismatch":
        values[10]["item"] = dict(values[10]["item"], summary=[])
    elif mutation == "terminal_unknown":
        values[-1]["response"]["output"].append({"id": "unknown", "type": "reasoning", "summary": []})
    elif mutation == "terminal_summary_mismatch":
        values[-1]["response"]["output"][0] = dict(values[-1]["response"]["output"][0], summary=[])
    elif mutation == "after_terminal":
        values.append(copy.deepcopy(values[-1]))
    else:
        values[1]["item"]["content"] = [{"type": "reasoning_text", "text": "not supported"}]
    with pytest.raises((GatewayError, ValueError)):
        await canonical(values)


def test_reasoning_replay_is_bounded_and_rejects_raw_content():
    codec = ResponsesCodec()
    item = {"type": "reasoning", "id": "rs1", "summary": []}
    for extra in ({"summary": [{"type": "summary_text", "text": ""}]*129},
                  {"summary": [{"type": "summary_text", "text": "é"*(REASONING_BYTE_LIMIT//2+1)}]},
                  {"encrypted_content": "x"*(REASONING_BYTE_LIMIT+1)},
                  {"content": [{"type": "reasoning_text", "text": "raw"}]}):
        with pytest.raises(GatewayError):
            codec.decode({"model": "m", "input": [dict(item, **extra)]}, {})


@pytest.mark.parametrize("protocol", ["chat", "anthropic", "gemini", "ollama"])
def test_reasoning_replay_does_not_get_translated_to_another_upstream(protocol):
    from autobuild_json.gateway.protocols.openai_chat import OpenAIChatCodec
    from autobuild_json.gateway.protocols.anthropic import AnthropicCodec
    from autobuild_json.gateway.protocols.gemini import GeminiCodec
    from autobuild_json.gateway.protocols.ollama import OllamaCodec
    request = ResponsesCodec().decode({"model": "m", "input": [
        {"type": "reasoning", "id": "rs1", "summary": [], "encrypted_content": "opaque"}]}, {})
    codec = {"chat": OpenAIChatCodec, "anthropic": AnthropicCodec, "gemini": GeminiCodec, "ollama": OllamaCodec}[protocol]()
    with pytest.raises(GatewayError, match="unsupported_feature"):
        codec.upstream_body(request, "upstream")
