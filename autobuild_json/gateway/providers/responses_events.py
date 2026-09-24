import json

from ..contracts import InferenceEvent, Text, ToolCall
from ..errors import GatewayError
from ..metering.usage import normalize_provider_usage
from ..protocols.frames import SSEDecoder


def normalize_responses_usage(payload):
    try:
        return normalize_provider_usage(payload, "openai_responses")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise GatewayError("invalid_usage", 502, "upstream") from None


async def responses_events(response):
    decoder = SSEDecoder()
    started, terminal, calls = False, False, False
    opened, closed = {}, set()
    try:
        async for chunk in response.chunks():
            for _, data in decoder.feed(chunk):
                value = json.loads(data)
                kind = value["type"]
                if terminal:
                    raise ValueError()
                if kind == "response.created":
                    if started:
                        raise ValueError()
                    started = True
                    yield InferenceEvent(kind="started", response_id=value["response"]["id"])
                elif kind == "response.output_item.added":
                    if not started:
                        raise ValueError()
                    item = value["item"]
                    if item["type"] == "message":
                        block = Text(text="")
                    elif item["type"] == "function_call":
                        block = ToolCall(call_id=item["call_id"], name=item["name"], arguments=item.get("arguments", ""))
                        calls = True
                    else:
                        raise GatewayError("unsupported_feature", 502, "stream")
                    opened[item["id"]] = value["output_index"]
                    yield InferenceEvent(kind="block_started", item_id=item["id"], index=value["output_index"], block=block)
                elif kind in {"response.output_text.delta", "response.function_call_arguments.delta"}:
                    yield InferenceEvent(kind="text_delta" if kind == "response.output_text.delta" else "tool_delta",
                                         item_id=value["item_id"], delta=value["delta"])
                elif kind == "response.output_item.done":
                    item_id = value["item"]["id"]
                    closed.add(item_id)
                    yield InferenceEvent(kind="block_finished", item_id=item_id)
                elif kind in {"response.completed", "response.incomplete"}:
                    if not started or closed != set(opened):
                        raise ValueError()
                    terminal = True
                    payload = value["response"]
                    usage = normalize_responses_usage(payload["usage"]) if payload.get("usage") is not None else None
                    if usage:
                        yield InferenceEvent(kind="usage", usage=usage)
                    yield InferenceEvent(kind="finished", finish_reason="length" if kind == "response.incomplete" else "tool_calls" if calls else "stop",
                                         usage=usage)
                elif kind in {"response.failed", "error"}:
                    raise GatewayError("upstream_error", 502, "stream")
                elif kind not in {"response.in_progress", "response.content_part.added", "response.content_part.done",
                                  "response.output_text.done", "response.function_call_arguments.done"}:
                    raise GatewayError("unsupported_feature", 502, "stream")
        decoder.finish()
        if not terminal:
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise GatewayError("upstream_error", 502, "stream") from None
