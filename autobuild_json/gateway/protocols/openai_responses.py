import json
import time
from uuid import uuid4

from ..contracts import InferenceRequest, Message, Text, Tool, ToolCall, ToolResult, Reasoning, GenerationOptions
from ..errors import GatewayError
from .common import EventCollector
from .responses_options import TextOptions


def response_usage(usage):
    if usage is None:
        return None
    return {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "total_tokens": usage.input_tokens+usage.output_tokens,
            "input_tokens_details": {"cached_tokens": usage.cached_read},
            "output_tokens_details": {"reasoning_tokens": usage.reasoning}}


def output_item(block, item_id, status="completed"):
    if isinstance(block, Reasoning):
        value = {"id": block.id, "type": "reasoning", "summary": [
            {"type": "summary_text", "text": part} for part in block.summary]}
        if block.status is not None:
            value["status"] = block.status
        if block.encrypted_content is not None:
            value["encrypted_content"] = block.encrypted_content
        return value
    if isinstance(block, Text):
        return {"id": item_id, "type": "message", "status": status, "role": "assistant",
                "content": [{"type": "output_text", "text": block.text, "annotations": []}]}
    if isinstance(block, ToolCall):
        return {"id": item_id, "type": "function_call", "status": status, "call_id": block.call_id,
                "name": block.name, "arguments": block.arguments}
    raise GatewayError("unsupported_feature")


def reasoning_item(item):
    """Read only published summaries and opaque replay data, never raw content."""
    if not isinstance(item, dict) or set(item)-{"type", "id", "summary", "encrypted_content", "status"} or item.get("type") != "reasoning":
        raise GatewayError("unsupported_feature")
    summary = item.get("summary", [])
    if not isinstance(summary, list) or any(not isinstance(part, dict) or set(part) != {"type", "text"}
        or part["type"] != "summary_text" for part in summary):
        raise ValueError("invalid_reasoning_summary")
    return Reasoning(id=item["id"], summary=tuple(part["text"] for part in summary),
                     encrypted_content=item.get("encrypted_content"), status=item.get("status"))


class ResponsesCodec:
    def __init__(self, response_id=None, model=""):
        self.response_id, self.model = response_id or "resp_"+uuid4().hex, model
        self.created = int(time.time())
        self.sequence = 0
        self.collector = EventCollector(self.response_id, model)

    def decode(self, body, options):
        allowed = {"model", "input", "instructions", "tools", "tool_choice", "max_output_tokens", "temperature",
                   "top_p", "stream", "store", "background", "parallel_tool_calls", "previous_response_id",
                   "reasoning", "include", "text", "prompt_cache_key"}
        if not isinstance(body, dict) or set(body)-allowed or body.get("store", False) is not False or body.get("background", False) is not False:
            raise GatewayError("unsupported_feature")
        try:
            items = body.get("input", [])
            if isinstance(items, str):
                items = [{"role": "user", "content": [{"type": "input_text", "text": items}]}]
            messages = []
            for item in items:
                kind = item.get("type", "message")
                if kind == "function_call":
                    messages.append(Message(role="assistant", blocks=(ToolCall(call_id=item["call_id"],
                        name=item["name"], arguments=item["arguments"]),)))
                elif kind == "function_call_output":
                    messages.append(Message(role="tool", blocks=(ToolResult(call_id=item["call_id"], content=item["output"]),)))
                elif kind == "reasoning":
                    messages.append(Message(role="assistant", blocks=(reasoning_item(item),)))
                elif kind == "message":
                    content = item["content"]
                    if isinstance(content, str):
                        blocks = (Text(text=content),)
                    else:
                        blocks = []
                        for block in content:
                            if block["type"] not in {"input_text", "output_text"} or set(block)-{"type", "text", "annotations"}:
                                raise GatewayError("unsupported_feature")
                            blocks.append(Text(text=block["text"]))
                    messages.append(Message(role=item["role"], blocks=blocks))
                else:
                    raise GatewayError("unsupported_feature")
            tools = []
            for tool in body.get("tools", []):
                if tool.get("type") != "function":
                    raise GatewayError("unsupported_feature")
                tools.append(Tool(**{k: v for k, v in tool.items() if k != "type"}))
            reasoning = body.get("reasoning")
            if reasoning is not None and (not isinstance(reasoning, dict) or set(reasoning)-{"effort", "summary", "context"}):
                raise GatewayError("unsupported_feature")
            include = body.get("include", ())
            if not isinstance(include, (list, tuple)) or any(not isinstance(item, str) or not item for item in include):
                raise GatewayError("invalid_request")
            request = InferenceRequest(model=body["model"], messages=messages, instructions=body.get("instructions"),
                tools=tools, stream=body.get("stream", False), continuation=body.get("previous_response_id"),
                options=GenerationOptions(max_output_tokens=body.get("max_output_tokens"), temperature=body.get("temperature"),
                    top_p=body.get("top_p"), tool_choice=body.get("tool_choice", "auto"), parallel_tool_calls=body.get("parallel_tool_calls"),
                    reasoning=reasoning, include=tuple(include),
                    text=TextOptions.model_validate(body["text"]).model_dump(exclude_none=True) if body.get("text") is not None else None,
                    prompt_cache_key=body.get("prompt_cache_key")))
            self.model = request.model
            self.collector = EventCollector(self.response_id, self.model)
            return request
        except GatewayError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError):
            raise GatewayError("invalid_request") from None

    def upstream_body(self, request, model):
        items = []
        for message in request.messages:
            text_blocks = []
            for block in message.blocks:
                if isinstance(block, Text):
                    text_blocks.append({"type": "output_text" if message.role == "assistant" else "input_text", "text": block.text})
                elif isinstance(block, ToolCall):
                    items.append({"type": "function_call", "call_id": block.call_id, "name": block.name, "arguments": block.arguments})
                elif isinstance(block, ToolResult):
                    items.append({"type": "function_call_output", "call_id": block.call_id, "output": block.content})
                elif isinstance(block, Reasoning):
                    if message.role != "assistant":
                        raise GatewayError("unsupported_feature")
                    items.append(output_item(block, block.id))
                else:
                    raise GatewayError("unsupported_feature")
            if text_blocks:
                items.append({"role": message.role, "content": text_blocks})
        body = {"model": model, "input": items, "stream": True, "store": False}
        if request.instructions is not None:
            body["instructions"] = request.instructions
        if request.continuation is not None:
            body["previous_response_id"] = request.continuation
        if request.tools:
            body["tools"] = [dict(type="function", **tool.model_dump(exclude_none=True)) for tool in request.tools]
            body["tool_choice"] = request.options.tool_choice
        if request.options.reasoning is not None:
            body["reasoning"] = request.options.reasoning.model_dump(exclude_none=True)
        if request.options.include:
            body["include"] = list(request.options.include)
        if request.options.text is not None:
            body["text"] = TextOptions.model_validate(request.options.text).model_dump(exclude_none=True)
        if request.options.prompt_cache_key is not None:
            body["prompt_cache_key"] = request.options.prompt_cache_key
        for name in ("max_output_tokens", "temperature", "top_p", "parallel_tool_calls"):
            value = getattr(request.options, name)
            if value is not None:
                body[name] = value
        if request.options.stop:
            raise GatewayError("unsupported_feature")
        return body

    def _base(self, status):
        return {"id": self.response_id, "object": "response", "created_at": self.created, "status": status,
                "model": self.model, "output": [], "error": None, "incomplete_details": None,
                "parallel_tool_calls": True, "tool_choice": "auto", "tools": [], "usage": None}

    def encode_result(self, result):
        value = self._base("incomplete" if result.finish_reason == "length" else "completed")
        value.update(id=result.id, model=result.model, output=[output_item(block, f"item_{index}") for index, block in enumerate(result.blocks)],
                     usage=response_usage(result.usage))
        if result.finish_reason == "length":
            value["incomplete_details"] = {"reason": "max_output_tokens"}
        return value

    def _event(self, kind, **data):
        value = {"type": kind, "sequence_number": self.sequence, **data}
        self.sequence += 1
        return ("event: "+kind+"\ndata: "+json.dumps(value, ensure_ascii=False, separators=(",", ":"))+"\n\n").encode()

    def encode_event(self, event):
        if event.kind == "started":
            self.response_id = event.response_id or self.response_id
            self.collector = EventCollector(self.response_id, self.model)
        self.collector.feed(event)
        if event.kind == "started":
            return [self._event("response.created", response=self._base("in_progress")),
                    self._event("response.in_progress", response=self._base("in_progress"))]
        if event.kind == "block_started":
            item = output_item(event.block, event.item_id, "in_progress")
            if isinstance(event.block, Reasoning):
                # Summary text arrives through summary-part events; item-added
                # must be an empty shell (never duplicate the first delta).
                item["summary"] = []
                item.pop("encrypted_content", None)
            frames = [self._event("response.output_item.added", output_index=event.index, item=item)]
            if isinstance(event.block, Text):
                frames.append(self._event("response.content_part.added", item_id=event.item_id, output_index=event.index,
                                          content_index=0, part={"type": "output_text", "text": "", "annotations": []}))
            return frames
        if event.kind in {"text_delta", "tool_delta"}:
            index, _ = self.collector.blocks[event.item_id]
            values = {"item_id": event.item_id, "output_index": index, "delta": event.delta}
            if event.kind == "text_delta":
                values["content_index"] = 0
            return [self._event("response.output_text.delta" if event.kind == "text_delta" else "response.function_call_arguments.delta", **values)]
        if event.kind in {"reasoning_summary_started", "reasoning_summary_delta", "reasoning_summary_finished"}:
            index, block = self.collector.blocks[event.item_id]
            values = {"item_id": event.item_id, "output_index": index, "summary_index": event.summary_index}
            part = {"type": "summary_text", "text": block.summary[event.summary_index]}
            if event.kind == "reasoning_summary_started":
                frames = [self._event("response.reasoning_summary_part.added", **values,
                                     part={"type": "summary_text", "text": ""})]
                if event.delta:
                    frames.append(self._event("response.reasoning_summary_text.delta", **values, delta=event.delta))
                return frames
            if event.kind == "reasoning_summary_delta":
                return [self._event("response.reasoning_summary_text.delta", **values, delta=event.delta)]
            return [self._event("response.reasoning_summary_text.done", **values, text=part["text"]),
                    self._event("response.reasoning_summary_part.done", **values, part=part)]
        if event.kind == "block_finished":
            index, block = self.collector.blocks[event.item_id]
            frames = []
            if isinstance(block, Text):
                frames.append(self._event("response.output_text.done", item_id=event.item_id, output_index=index, content_index=0, text=block.text))
                frames.append(self._event("response.content_part.done", item_id=event.item_id, output_index=index, content_index=0,
                                          part={"type": "output_text", "text": block.text, "annotations": []}))
            elif isinstance(block, ToolCall):
                frames.append(self._event("response.function_call_arguments.done", item_id=event.item_id,
                                          output_index=index, arguments=block.arguments))
            frames.append(self._event("response.output_item.done", output_index=index, item=output_item(block, event.item_id)))
            return frames
        if event.kind == "finished":
            response = self.encode_result(self.collector.result())
            response["output"] = [output_item(block, key) for key, (_, block) in sorted(self.collector.blocks.items(), key=lambda pair: pair[1][0])]
            return [self._event("response.incomplete" if event.finish_reason == "length" else "response.completed", response=response)]
        if event.kind == "error":
            return [self._event("error", code=event.error_code or "upstream_error", message=event.error_code or "upstream_error", param=None)]
        return []
