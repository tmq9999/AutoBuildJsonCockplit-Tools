import json
import logging

from ..contracts import InferenceEvent, Text, ToolCall, Reasoning, GeneratedImage, Compaction, Opaque
from ..errors import GatewayError, SAFE_CODES
from ..metering.usage import normalize_provider_usage
from ..protocols.common import EventCollector
from ..protocols.frames import SSEDecoder
from ..protocols.openai_responses import message_phase, reasoning_item, native_tool_item

logger = logging.getLogger(__name__)

# Diagnostic labels only: membership does not imply protocol support.
DIAGNOSTIC_EVENTS = frozenset({
    "error", "response.created", "response.in_progress", "response.completed",
    "response.incomplete", "response.failed", "response.queued",
    "response.output_item.added", "response.output_item.done",
    "response.content_part.added", "response.content_part.done",
    "response.output_text.delta", "response.output_text.done", "response.output_text.annotation.added",
    "response.function_call_arguments.delta", "response.function_call_arguments.done",
    "response.custom_tool_call_input.delta", "response.custom_tool_call_input.done",
    "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
    "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
    "response.reasoning_text.delta", "response.reasoning_text.done",
    "response.refusal.delta", "response.refusal.done", "response.compaction.compacting",
    "response.image_generation_call.in_progress", "response.image_generation_call.generating",
    "response.image_generation_call.completed", "response.image_generation_call.partial_image",
    "response.web_search_call.in_progress", "response.web_search_call.searching",
    "response.web_search_call.completed", "codex.rate_limits", "codex.response.metadata",
    "responsesapi.websocket_timing",
})
DIAGNOSTIC_ITEMS = frozenset({
    "message", "reasoning", "function_call", "custom_tool_call", "image_generation_call",
    "web_search_call", "file_search_call", "code_interpreter_call", "mcp_call",
    "mcp_list_tools", "shell_call", "local_shell_call", "compaction",
})
DIAGNOSTIC_FIELDS = frozenset({
    "id", "type", "status", "content", "summary", "encrypted_content", "metadata",
    "internal_chat_message_metadata_passthrough", "channel", "recipient", "phase", "role",
    "signature", "format", "visibility", "name", "namespace", "input", "arguments",
    "call_id", "caller", "async",
})


def normalize_responses_usage(payload):
    try:
        return normalize_provider_usage(payload, "openai_responses")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise GatewayError("invalid_usage", 502, "upstream") from None


def safe_upstream_error_code(value):
    """Keep an upstream lifecycle error's code without exposing its message."""
    candidates = [value.get("code")] if isinstance(value, dict) else []
    nested = value.get("error") if isinstance(value, dict) else None
    if isinstance(nested, dict):
        candidates.append(nested.get("code"))
    return next((code for code in candidates if isinstance(code, str) and code in SAFE_CODES), "upstream_error")


class ResponsesEvents:
    """Validate provider item/summary lifecycles before publishing events."""

    def __init__(self, *, allow_empty_terminal_output=False):
        self.collector = EventCollector("", "")
        self.allow_empty_terminal_output = allow_empty_terminal_output
        self.calls = False
        self.summary_text_done = set()
        self.last_event_type = "none"
        self.last_item_type = "none"
        self.last_item_fields = ""
        self.other_item_fields = 0

    def _item(self, value, item_id=None):
        identity = item_id if item_id is not None else value["item_id"]
        if identity not in self.collector.blocks or identity in self.collector.closed:
            raise ValueError("invalid_block")
        index, block = self.collector.blocks[identity]
        if "output_index" in value and (type(value["output_index"]) is not int or value["output_index"] != index):
            raise ValueError("invalid_output_index")
        return identity, block

    def feed(self, value):
        kind = value["type"]
        self.last_event_type = kind if isinstance(kind, str) and kind in DIAGNOSTIC_EVENTS else "other"
        item = value.get("item")
        item_kind = item.get("type") if isinstance(item, dict) else None
        self.last_item_type = (item_kind if isinstance(item_kind, str) and item_kind in DIAGNOSTIC_ITEMS
                               else "other" if item is not None else "none")
        fields = set(item) if isinstance(item, dict) else set()
        self.last_item_fields = ",".join(sorted(fields & DIAGNOSTIC_FIELDS))
        self.other_item_fields = len(fields - DIAGNOSTIC_FIELDS)
        if self.collector.terminal:
            raise ValueError("event_after_terminal")
        # A provider can fail before it emits response.created. Preserve the
        # safe error code without treating an HTTP 200 SSE error as retryable.
        if kind in {"response.failed", "error"}:
            payload = value.get("response", {}) if kind == "response.failed" else value
            raise GatewayError(safe_upstream_error_code(payload), 502, "stream")
        events = []
        if kind == "response.created":
            events.append(InferenceEvent(kind="started", response_id=value["response"]["id"]))
        elif not self.collector.started:
            raise ValueError("missing_start")
        elif kind == "response.output_item.added":
            item = value["item"]
            if type(value["output_index"]) is not int:
                raise ValueError("invalid_output_index")
            if item["type"] == "message":
                block = Text(text="", phase=message_phase(item))
            elif item["type"] == "custom_tool_call" or (item["type"] == "function_call" and
                    {"namespace", "caller", "async", "metadata", "internal_chat_message_metadata_passthrough"} & set(item)):
                block = native_tool_item(item)
                self.calls = True
            elif item["type"] == "function_call":
                block = ToolCall(call_id=item["call_id"], name=item["name"], arguments=item.get("arguments", ""))
                self.calls = True
            elif item["type"] == "reasoning":
                block = reasoning_item(item)
            elif item["type"] == "image_generation_call":
                block = GeneratedImage.model_validate(dict(item, type="generated_image"))
            else:
                raise GatewayError("unsupported_feature", 502, "stream")
            events.append(InferenceEvent(kind="block_started", item_id=item["id"], index=value["output_index"], block=block))
        elif kind in {"response.output_text.delta", "response.function_call_arguments.delta", "response.custom_tool_call_input.delta"}:
            identity, block = self._item(value)
            if kind == "response.custom_tool_call_input.delta" and not (isinstance(block, Opaque) and block.tool_input_field == "input"):
                raise ValueError("invalid_tool_delta")
            if kind == "response.function_call_arguments.delta" and isinstance(block, Opaque) and block.tool_input_field != "arguments":
                raise ValueError("invalid_tool_delta")
            events.append(InferenceEvent(kind="text_delta" if kind == "response.output_text.delta" else "tool_delta",
                                         item_id=identity, delta=value["delta"]))
        elif kind in {"response.reasoning_summary_part.added", "response.reasoning_summary_text.delta",
                      "response.reasoning_summary_text.done", "response.reasoning_summary_part.done"}:
            identity, block = self._item(value)
            part_index = value["summary_index"]
            if not isinstance(block, Reasoning) or type(part_index) is not int or not 0 <= part_index < 128:
                raise ValueError("invalid_reasoning_event")
            key = (identity, part_index)
            if kind == "response.reasoning_summary_part.added":
                part = value["part"]
                if not isinstance(part, dict) or set(part) != {"type", "text"} or part["type"] != "summary_text":
                    raise ValueError("invalid_reasoning_part")
                events.append(InferenceEvent(kind="reasoning_summary_started", item_id=identity,
                                             summary_index=part_index, delta=part["text"]))
            elif kind == "response.reasoning_summary_text.delta":
                if key in self.summary_text_done:
                    raise ValueError("delta_after_done")
                events.append(InferenceEvent(kind="reasoning_summary_delta", item_id=identity,
                                             summary_index=part_index, delta=value["delta"]))
            elif kind == "response.reasoning_summary_text.done":
                if (key in self.summary_text_done or part_index >= len(block.summary)
                        or part_index in self.collector.reasoning_parts[identity] or value["text"] != block.summary[part_index]):
                    raise ValueError("invalid_reasoning_done")
                self.summary_text_done.add(key)
            else:
                part = value["part"]
                if key not in self.summary_text_done or not isinstance(part, dict) or set(part) != {"type", "text"} or part["type"] != "summary_text":
                    raise ValueError("invalid_reasoning_part")
                events.append(InferenceEvent(kind="reasoning_summary_finished", item_id=identity,
                                             summary_index=part_index, delta=part["text"]))
        elif kind == "response.image_generation_call.partial_image":
            identity, block = self._item(value)
            if not isinstance(block, GeneratedImage):
                raise ValueError("invalid_image")
            events.append(InferenceEvent(kind="image_partial", item_id=identity,
                index=self.collector.blocks[identity][0], summary_index=value["partial_image_index"],
                block=GeneratedImage(id=identity, result=value["partial_image_b64"])))
        elif kind in {"response.image_generation_call.in_progress", "response.image_generation_call.generating",
                      "response.image_generation_call.completed"}:
            _, block = self._item(value)
            if not isinstance(block, GeneratedImage):
                raise ValueError("invalid_image")
        elif kind == "response.output_item.done":
            item = value["item"]
            identity, block = self._item(value, item["id"])
            expected = (block.payload["type"] if isinstance(block, Opaque) and block.tool_input_field else
                "image_generation_call" if isinstance(block, GeneratedImage) else
                "reasoning" if isinstance(block, Reasoning) else "function_call" if isinstance(block, ToolCall) else "message")
            if item.get("type") != expected:
                raise ValueError("invalid_item_type")
            final = reasoning_item(item) if isinstance(block, Reasoning) else None
            if isinstance(block, Opaque) and block.tool_input_field:
                final = native_tool_item(item)
            if isinstance(block, Text):
                phase = message_phase(item)
                if block.phase is not None and phase != block.phase:
                    raise ValueError("message_phase_mismatch")
                final = Text(text=block.text, phase=phase)
            if isinstance(block, GeneratedImage):
                final = GeneratedImage.model_validate(dict(item, type="generated_image"))
                if final.status != "completed" or not final.result:
                    raise ValueError("image_missing")
            events.append(InferenceEvent(kind="block_finished", item_id=identity, block=final))
        elif kind in {"response.completed", "response.incomplete"}:
            payload = value["response"]
            if "output" in payload:
                output = payload["output"]
                ordered = sorted(self.collector.blocks, key=lambda identity: self.collector.blocks[identity][0])
                # Codex sometimes emits an empty terminal ``output`` array
                # after the item lifecycle has already delivered
                # ``response.output_item.done``.  The lifecycle is the
                # authoritative stream in that case; rejecting it turns a
                # successful upstream response into a false 502.
                streamed_only = self.allow_empty_terminal_output and output == []
                if not isinstance(output, list) or (not streamed_only and [item["id"] for item in output] != ordered):
                    raise ValueError("invalid_terminal_output")
                for item in output:
                    block = self.collector.blocks[item["id"]][1]
                    if isinstance(block, Reasoning) and reasoning_item(item) != block:
                        raise ValueError("reasoning_terminal_mismatch")
                    if isinstance(block, Text) and message_phase(item) != block.phase:
                        raise ValueError("message_phase_mismatch")
                    if isinstance(block, GeneratedImage) and GeneratedImage.model_validate(dict(item, type="generated_image")) != block:
                        raise ValueError("image_terminal_mismatch")
                    if isinstance(block, Opaque) and block.tool_input_field and native_tool_item(item) != block:
                        raise ValueError("tool_terminal_mismatch")
            usage = normalize_responses_usage(payload["usage"]) if payload.get("usage") is not None else None
            if usage:
                events.append(InferenceEvent(kind="usage", usage=usage))
            events.append(InferenceEvent(kind="finished", finish_reason="length" if kind == "response.incomplete"
                                         else "tool_calls" if self.calls else "stop", usage=usage))
        elif kind == "response.custom_tool_call_input.done":
            _, block = self._item(value)
            if not isinstance(block, Opaque) or block.tool_input_field != "input" or value["input"] != block.payload["input"]:
                raise ValueError("invalid_tool_done")
        elif kind in {"response.content_part.added", "response.content_part.done", "response.output_text.done",
                      "response.function_call_arguments.done"}:
            _, block = self._item(value)
            if isinstance(block, Reasoning):
                raise ValueError("invalid_reasoning_event")
        elif kind != "response.in_progress":
            raise GatewayError("unsupported_feature", 502, "stream")
        for event in events:
            self.collector.feed(event)
        return events


async def responses_events(response, *, images=False, allow_empty_terminal_output=False):
    decoder = SSEDecoder(max_frame_bytes=40_000_000 if images else 1_048_576)
    parser = ResponsesEvents(allow_empty_terminal_output=allow_empty_terminal_output)
    try:
        async for chunk in response.chunks():
            for _, data in decoder.feed(chunk):
                for event in parser.feed(json.loads(data)):
                    yield event
        decoder.finish()
        if not parser.collector.terminal:
            raise ValueError("incomplete_output")
    except GatewayError as exc:
        trace = exc.__traceback__
        while trace.tb_next is not None:
            trace = trace.tb_next
        origin = trace.tb_frame.f_code.co_name
        if origin not in {"feed", "native_tool_item", "reasoning_item", "normalize_responses_usage"}:
            origin = "other"
        logger.warning("responses stream rejected code=%s event_type=%s item_type=%s origin=%s line=%d fields=%s other_fields=%d",
                       exc.code, parser.last_event_type, parser.last_item_type, origin, trace.tb_lineno,
                       parser.last_item_fields, parser.other_item_fields)
        raise
    except (ValueError, KeyError, TypeError, AttributeError):
        logger.warning("responses stream invalid code=upstream_error event_type=%s item_type=%s",
                       parser.last_event_type, parser.last_item_type)
        raise GatewayError("upstream_error", 502, "stream") from None


async def compact_events(response):
    """Turn a compact JSON result into the same metered lifecycle as generation."""
    try:
        value = await response.read_json(max_bytes=8_388_608)
        if value.get("object") != "response.compaction" or not isinstance(value.get("output"), list):
            raise ValueError("invalid_compaction")
        yield InferenceEvent(kind="started", response_id=value["id"])
        for index, item in enumerate(value["output"]):
            kind = item.get("type")
            if kind == "compaction":
                block = Compaction.model_validate(item)
            elif kind == "reasoning":
                block = reasoning_item(item)
            elif kind == "message":
                # Compaction retains user messages too; preserve these verbatim
                # in the compact codec, not as generated assistant text.
                from ..contracts import Opaque
                if item.get("role") not in {"system", "developer", "user", "assistant"}:
                    raise ValueError("invalid_message")
                block = Opaque(provider="codex_oauth", payload=item)
            else:
                raise ValueError("invalid_compaction_item")
            identity = item.get("id", "compact_item_"+str(index))
            yield InferenceEvent(kind="block_started", item_id=identity, index=index, block=block)
            yield InferenceEvent(kind="block_finished", item_id=identity)
        usage = normalize_responses_usage(value["usage"]) if value.get("usage") is not None else None
        yield InferenceEvent(kind="finished", finish_reason="stop", usage=usage)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise GatewayError("upstream_error", 502, "upstream") from None
