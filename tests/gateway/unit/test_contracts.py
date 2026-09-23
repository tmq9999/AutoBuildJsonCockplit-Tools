import pytest
from pydantic import ValidationError


def test_unknown_semantic_option_is_rejected():
    from autobuild_json.gateway.contracts import InferenceRequest
    with pytest.raises(ValidationError):
        InferenceRequest(model="m", messages=[], options={"background": True})


def test_interleaved_tool_fragments_keep_call_ids_and_parse_only_when_done():
    from autobuild_json.gateway.contracts import InferenceEvent, ToolCall
    from autobuild_json.gateway.protocols.common import EventCollector
    collector = EventCollector("response", "model")
    collector.feed(InferenceEvent(kind="started"))
    collector.feed(InferenceEvent(kind="block_started", item_id="a", index=0,
                                   block=ToolCall(call_id="a", name="alpha", arguments="")))
    collector.feed(InferenceEvent(kind="block_started", item_id="b", index=1,
                                   block=ToolCall(call_id="b", name="beta", arguments="")))
    collector.feed(InferenceEvent(kind="tool_delta", item_id="a", delta='{"x":'))
    collector.feed(InferenceEvent(kind="tool_delta", item_id="b", delta='{"y":2}'))
    collector.feed(InferenceEvent(kind="tool_delta", item_id="a", delta='1}'))
    for key in ("a", "b"):
        collector.feed(InferenceEvent(kind="block_finished", item_id=key))
    collector.feed(InferenceEvent(kind="finished", finish_reason="tool_calls"))
    result = collector.result()
    assert [(b.call_id, b.arguments) for b in result.blocks] == [("a", '{"x":1}'), ("b", '{"y":2}')]
    with pytest.raises(ValueError):
        collector.feed(InferenceEvent(kind="finished", finish_reason="stop"))


def test_incomplete_tool_json_and_missing_terminal_are_errors():
    from autobuild_json.gateway.contracts import InferenceEvent, ToolCall
    from autobuild_json.gateway.protocols.common import EventCollector
    collector = EventCollector("r", "m")
    collector.feed(InferenceEvent(kind="started"))
    collector.feed(InferenceEvent(kind="block_started", item_id="x", index=0,
                                   block=ToolCall(call_id="x", name="f", arguments="")))
    collector.feed(InferenceEvent(kind="tool_delta", item_id="x", delta="{"))
    with pytest.raises(ValueError):
        collector.feed(InferenceEvent(kind="block_finished", item_id="x"))
    with pytest.raises(ValueError):
        collector.result()


def test_signed_blocks_require_same_provider():
    from autobuild_json.gateway.contracts import InferenceRequest, Message, Opaque
    request = InferenceRequest(model="m", messages=[Message(role="assistant",
        blocks=[Opaque(provider="anthropic", payload={"signature": "synthetic"})])])
    with pytest.raises(ValueError):
        request.validate_provider("openai_compatible")
    request.validate_provider("anthropic")


def test_empty_success_is_valid_but_terminal_error_is_not_success():
    from autobuild_json.gateway.contracts import InferenceEvent
    from autobuild_json.gateway.protocols.common import EventCollector
    good = EventCollector("r", "m")
    good.feed(InferenceEvent(kind="started"))
    good.feed(InferenceEvent(kind="finished", finish_reason="stop"))
    assert good.result().blocks == ()
    bad = EventCollector("r", "m")
    bad.feed(InferenceEvent(kind="started"))
    bad.feed(InferenceEvent(kind="error", error_code="upstream_error"))
    with pytest.raises(ValueError):
        bad.result()


def test_tool_argument_limit_is_enforced_before_terminal():
    from autobuild_json.gateway.contracts import InferenceEvent, ToolCall
    from autobuild_json.gateway.protocols.common import EventCollector
    collector = EventCollector("r", "m")
    collector.feed(InferenceEvent(kind="started"))
    collector.feed(InferenceEvent(kind="block_started", item_id="x", block=ToolCall(call_id="x", name="f")))
    with pytest.raises(ValueError, match="tool_limit_exceeded"):
        collector.feed(InferenceEvent(kind="tool_delta", item_id="x", delta="x"*1_048_577))
