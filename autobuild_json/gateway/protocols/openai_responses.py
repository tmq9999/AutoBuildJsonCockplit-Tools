import json
import time
from uuid import uuid4

from ..contracts import (InferenceRequest, Message, Text, Tool, ToolCall, ToolResult, Reasoning,
                         GenerationOptions, Image, Compaction, GeneratedImage, ImageGenerationTool, Opaque)
from ..errors import GatewayError, SAFE_CODES
from .common import EventCollector
from .responses_options import TextOptions


def response_usage(usage):
    if usage is None:
        return None
    return {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "total_tokens": usage.input_tokens+usage.output_tokens,
            "input_tokens_details": {"cached_tokens": usage.cached_read,
                                     **({"cache_write_tokens": usage.cached_write} if usage.cached_write else {})},
            "output_tokens_details": {"reasoning_tokens": usage.reasoning}}


def output_item(block, item_id, status="completed"):
    if isinstance(block, Compaction):
        return block.model_dump()
    if isinstance(block, Opaque):
        return block.payload
    if isinstance(block, GeneratedImage):
        return dict(block.model_dump(exclude_none=True), type="image_generation_call")
    if isinstance(block, Reasoning):
        value = {"id": block.id, "type": "reasoning", "summary": [
            {"type": "summary_text", "text": part} for part in block.summary]}
        if block.status is not None:
            value["status"] = block.status
        if block.encrypted_content is not None:
            value["encrypted_content"] = block.encrypted_content
        return value
    if isinstance(block, Text):
        value = {"id": item_id, "type": "message", "status": status, "role": "assistant",
                 "content": [{"type": "output_text", "text": block.text, "annotations": []}]}
        if block.phase is not None:
            value["phase"] = block.phase
        return value
    if isinstance(block, ToolCall):
        return {"id": item_id, "type": "function_call", "status": status, "call_id": block.call_id,
                "name": block.name, "arguments": block.arguments}
    raise GatewayError("unsupported_feature")


def message_phase(item):
    phase = item.get("phase")
    if phase is None:
        return None
    if item.get("role") != "assistant" or phase not in {"commentary", "final_answer"}:
        raise ValueError("invalid_message_phase")
    return phase


def caller_value(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"type", "caller_id"}:
        raise ValueError("invalid_tool_caller")
    caller_type = value.get("type")
    if caller_type not in {"direct", "program"}:
        raise ValueError("invalid_tool_caller")
    if caller_type == "program" and (not isinstance(value.get("caller_id"), str)
                                      or not 1 <= len(value["caller_id"]) <= 512):
        raise ValueError("invalid_tool_caller")
    if "caller_id" in value and (not isinstance(value["caller_id"], str)
                                  or not 1 <= len(value["caller_id"]) <= 512):
        raise ValueError("invalid_tool_caller")
    return value


def custom_tool_output_part(part):
    if not isinstance(part, dict) or not isinstance(part.get("type"), str):
        raise ValueError("invalid_tool_output")
    kind = part["type"]
    fields = {
        "input_text": {"type", "text", "prompt_cache_breakpoint"},
        "input_image": {"type", "detail", "file_id", "image_url", "prompt_cache_breakpoint"},
        "input_file": {"type", "detail", "file_data", "file_id", "file_url", "filename",
                       "prompt_cache_breakpoint"},
    }.get(kind)
    if fields is None or set(part) - fields:
        raise GatewayError("unsupported_feature")
    for name in {"file_id", "image_url", "file_data", "file_url"} & set(part):
        value = part[name]
        if value is None and name in {"file_id", "image_url"}:
            continue
        limit = 512 if name == "file_id" else 2 * 1024 * 1024
        if not isinstance(value, str) or not 1 <= len(value.encode()) <= limit:
            raise ValueError("invalid_tool_output")
    if kind == "input_text":
        if not isinstance(part.get("text"), str) or len(part["text"].encode()) > 1_048_576:
            raise ValueError("invalid_tool_output")
    elif kind == "input_image":
        if not any(isinstance(part.get(name), str) and part[name] for name in ("file_id", "image_url")):
            raise ValueError("invalid_tool_output")
        if "detail" in part and part["detail"] not in {"auto", "low", "high", "original"}:
            raise ValueError("invalid_tool_output")
    else:
        if not any(isinstance(part.get(name), str) and part[name]
                   for name in ("file_data", "file_id", "file_url")):
            raise ValueError("invalid_tool_output")
        if "detail" in part and part["detail"] not in {"auto", "low", "high"}:
            raise ValueError("invalid_tool_output")
        if "filename" in part and (not isinstance(part["filename"], str)
                                    or not 1 <= len(part["filename"]) <= 512):
            raise ValueError("invalid_tool_output")
    if "prompt_cache_breakpoint" in part:
        breakpoint = part["prompt_cache_breakpoint"]
        if breakpoint is not None and breakpoint != {"mode": "explicit"}:
            raise ValueError("invalid_tool_output")
    return part


def reasoning_item(item):
    """Read only published summaries and opaque replay data, never raw content."""
    metadata_fields = {"metadata", "internal_chat_message_metadata_passthrough"}
    allowed = {"type", "id", "summary", "encrypted_content", "status", "content"} | metadata_fields
    if not isinstance(item, dict) or set(item) - allowed or item.get("type") != "reasoning":
        raise GatewayError("unsupported_feature")
    # Codex decorates reasoning items with transport metadata. Bound it but do
    # not log, persist or replay it as reasoning; retain only public fields.
    try:
        metadata_bytes = json.dumps({name: item[name] for name in metadata_fields if name in item},
                                    ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError):
        raise ValueError("invalid_reasoning_metadata") from None
    if len(metadata_bytes) > 256 * 1024:
        raise ValueError("reasoning_metadata_limit_exceeded")
    summary = item.get("summary", [])
    if not isinstance(summary, list) or any(not isinstance(part, dict) or set(part) != {"type", "text"}
        or part["type"] != "summary_text" for part in summary):
        raise ValueError("invalid_reasoning_summary")
    content = item.get("content", [])
    # The live Codex item includes this field but currently leaves it empty.
    # Non-empty raw reasoning is intentionally rejected: it is not a portable
    # public summary and must never be persisted or exposed by another wire API.
    if content != []:
        raise ValueError("invalid_reasoning_content")
    return Reasoning(id=item["id"], summary=tuple(part["text"] for part in summary),
                     encrypted_content=item.get("encrypted_content"), status=item.get("status"))


def chat_messages_input(body):
    """Translate the common Chat Completions message envelope to Responses input.

    Some OpenAI-compatible clients select the Responses URL while retaining a
    ``messages`` body.  Keep that compatibility at the inbound boundary, then
    continue through the same canonical message validation as native Responses
    input.  Native Responses fields remain handled by the caller.
    """
    from .openai_chat import OpenAIChatCodec

    parsed = OpenAIChatCodec().decode({"model": body["model"], "messages": body["messages"]}, {})
    items = []
    for message in parsed.messages:
        text_blocks = []
        tool_blocks = []
        for block in message.blocks:
            if isinstance(block, Text):
                text_blocks.append({"type": "output_text" if message.role == "assistant" else "input_text",
                                    "text": block.text})
            elif isinstance(block, ToolCall):
                tool_blocks.append({"type": "function_call", "call_id": block.call_id,
                                    "name": block.name, "arguments": block.arguments})
            elif isinstance(block, ToolResult):
                tool_blocks.append({"type": "function_call_output", "call_id": block.call_id,
                                    "output": block.content})
            else:
                raise GatewayError("unsupported_feature")
        if text_blocks or not tool_blocks:
            items.append({"type": "message", "role": message.role, "content": text_blocks})
        items.extend(tool_blocks)
    return items


def additional_tools_item(item):
    """Validate and retain Codex's provider-specific built-in tool item.

    Codex places namespaces such as ``web_search``/``shell`` in an
    ``additional_tools`` input item rather than the top-level Responses
    ``tools`` array.  The gateway does not interpret those definitions; it
    forwards the bounded JSON only to the Codex adapter as an opaque item.
    """
    if not isinstance(item, dict) or set(item) - {"type", "role", "id", "tools"}:
        raise GatewayError("unsupported_feature")
    if item.get("type") != "additional_tools" or item.get("role") != "developer":
        raise GatewayError("unsupported_feature")
    if "id" in item and (not isinstance(item["id"], str) or not 1 <= len(item["id"]) <= 512):
        raise GatewayError("invalid_request")
    tools = item.get("tools")
    if not isinstance(tools, list) or not 1 <= len(tools) <= 128 or any(not isinstance(tool, dict) for tool in tools):
        raise GatewayError("invalid_request")
    try:
        encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise GatewayError("invalid_request") from None
    if len(encoded) > 2 * 1024 * 1024:
        raise GatewayError("invalid_request")
    return json.loads(encoded)


def native_tool_item(item):
    """Retain Codex grammar calls/namespaces without pretending they are JSON functions."""
    kind = item.get("type")
    allowed = {"type", "id", "call_id", "status"}
    if kind in {"custom_tool_call", "function_call"}:
        field = "input" if kind == "custom_tool_call" else "arguments"
        allowed.update({"name", "namespace", "metadata", "internal_chat_message_metadata_passthrough",
                        "async", "caller", field})
        if not isinstance(item.get(field), str):
            raise ValueError("invalid_tool_input")
        if not isinstance(item.get("name"), str) or not 1 <= len(item["name"]) <= 128:
            raise ValueError("invalid_tool_name")
        if "namespace" in item and (not isinstance(item["namespace"], str) or not 1 <= len(item["namespace"]) <= 128):
            raise ValueError("invalid_tool_namespace")
        if "async" in item and type(item["async"]) is not bool:
            raise ValueError("invalid_tool_async")
        if "caller" in item:
            caller_value(item["caller"])
        if "metadata" in item:
            if not isinstance(item["metadata"], dict):
                raise ValueError("invalid_tool_metadata")
            try:
                metadata_size = len(json.dumps(item["metadata"], ensure_ascii=False,
                                               separators=(",", ":"), allow_nan=False).encode())
            except (TypeError, ValueError):
                raise ValueError("invalid_tool_metadata") from None
            if metadata_size > 256 * 1024:
                raise ValueError("tool_limit_exceeded")
    elif kind == "custom_tool_call_output":
        allowed.update({"output", "async", "caller"})
        output = item.get("output")
        if not isinstance(output, (str, list)):
            raise ValueError("invalid_tool_output")
        if isinstance(output, list):
            if not 1 <= len(output) <= 128:
                raise ValueError("invalid_tool_output")
            for part in output:
                custom_tool_output_part(part)
        if isinstance(output, str) and len(output.encode()) > 1_048_576:
            raise ValueError("invalid_tool_output")
        if "async" in item and type(item["async"]) is not bool:
            raise ValueError("invalid_tool_async")
        if "caller" in item:
            caller_value(item["caller"])
    else:
        raise GatewayError("unsupported_feature")
    if set(item) - allowed:
        raise GatewayError("unsupported_feature")
    if not isinstance(item.get("call_id"), str) or not 1 <= len(item["call_id"]) <= 512:
        raise ValueError("invalid_tool_call_id")
    if "id" in item and (not isinstance(item["id"], str) or not 1 <= len(item["id"]) <= 512):
        raise ValueError("invalid_tool_id")
    if "status" in item and item["status"] not in {"in_progress", "completed", "incomplete"}:
        raise ValueError("invalid_tool_status")
    encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > 2 * 1024 * 1024:
        raise ValueError("tool_limit_exceeded")
    return Opaque(provider="codex_oauth", payload=json.loads(encoded))


class ResponsesCodec:
    def __init__(self, response_id=None, model=""):
        self.response_id, self.model = response_id or "resp_"+uuid4().hex, model
        self.created = int(time.time())
        self.sequence = 0
        self.collector = EventCollector(self.response_id, model)
        self.response_tools = []
        self.response_tool_choice = "auto"
        self.response_parallel_tool_calls = True

    def decode(self, body, options):
        allowed = {"model", "input", "messages", "instructions", "tools", "tool_choice", "max_output_tokens", "temperature",
                   "top_p", "stream", "store", "background", "parallel_tool_calls", "previous_response_id",
                   "reasoning", "include", "text", "prompt_cache_key", "client_metadata", "service_tier"}
        if not isinstance(body, dict) or set(body)-allowed or body.get("store", False) is not False or body.get("background", False) is not False:
            raise GatewayError("unsupported_feature")
        if "input" in body and "messages" in body:
            raise GatewayError("invalid_request")
        if "messages" in body and not isinstance(body["messages"], list):
            raise GatewayError("invalid_request")
        try:
            items = chat_messages_input(body) if "messages" in body else body.get("input", [])
            if isinstance(items, str):
                items = [{"role": "user", "content": [{"type": "input_text", "text": items}]}]
            messages = []
            for item in items:
                kind = item.get("type", "message")
                if kind == "additional_tools":
                    messages.append(Message(role="developer", blocks=(Opaque(
                        provider="codex_oauth", payload=additional_tools_item(item)),)))
                elif kind in {"custom_tool_call", "custom_tool_call_output"} or (
                        kind == "function_call" and {"namespace", "caller", "async", "metadata",
                            "internal_chat_message_metadata_passthrough"} & set(item)):
                    messages.append(Message(role="tool" if kind.endswith("_output") else "assistant",
                                            blocks=(native_tool_item(item),)))
                elif kind == "function_call":
                    messages.append(Message(role="assistant", blocks=(ToolCall(call_id=item["call_id"],
                        name=item["name"], arguments=item["arguments"]),)))
                elif kind == "function_call_output":
                    messages.append(Message(role="tool", blocks=(ToolResult(call_id=item["call_id"], content=item["output"]),)))
                elif kind == "reasoning":
                    messages.append(Message(role="assistant", blocks=(reasoning_item(item),)))
                elif kind == "compaction":
                    messages.append(Message(role="assistant", blocks=(Compaction.model_validate(item),)))
                elif kind == "image_generation_call":
                    messages.append(Message(role="assistant", blocks=(GeneratedImage.model_validate(
                        dict(item, type="generated_image")),)))
                elif kind == "message":
                    phase = message_phase(item)
                    content = item["content"]
                    if isinstance(content, str):
                        blocks = (Text(text=content),)
                    else:
                        blocks = []
                        for block in content:
                            if block["type"] == "input_image":
                                if set(block)-{"type", "image_url", "detail"}:
                                    raise GatewayError("unsupported_feature")
                                source = image_source(block["image_url"])
                                blocks.append(Image(source=source, media_type=source[5:].split(";", 1)[0]
                                    if source.startswith("data:") else "image/*", detail=block.get("detail", "auto")))
                                continue
                            if block["type"] not in {"input_text", "output_text"} or set(block)-{"type", "text", "annotations"}:
                                raise GatewayError("unsupported_feature")
                            blocks.append(Text(text=block["text"]))
                    messages.append(Message(role=item["role"], blocks=blocks, phase=phase))
                else:
                    raise GatewayError("unsupported_feature")
            tools, image_tool = [], None
            for tool in body.get("tools", []):
                if tool.get("type") == "image_generation":
                    if image_tool is not None:
                        raise GatewayError("invalid_request")
                    image_tool = ImageGenerationTool.model_validate(tool)
                    if image_tool.input_image_mask is not None:
                        if set(image_tool.input_image_mask) != {"image_url"}:
                            raise GatewayError("invalid_request")
                        image_source(image_tool.input_image_mask["image_url"])
                    continue
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
                tools=tools, image_tool=image_tool, stream=body.get("stream", False), continuation=body.get("previous_response_id"),
                client_metadata=body.get("client_metadata"), service_tier=body.get("service_tier"),
                options=GenerationOptions(max_output_tokens=body.get("max_output_tokens"), temperature=body.get("temperature"),
                    top_p=body.get("top_p"), tool_choice=body.get("tool_choice", "auto"), parallel_tool_calls=body.get("parallel_tool_calls"),
                    reasoning=reasoning, include=tuple(include),
                    text=TextOptions.model_validate(body["text"]).model_dump(exclude_none=True) if body.get("text") is not None else None,
                    prompt_cache_key=body.get("prompt_cache_key")))
            self.model = request.model
            self.collector = EventCollector(self.response_id, self.model)
            self.response_tools = [dict(type="function", **tool.model_dump(exclude_none=True))
                                   for tool in request.tools]
            if request.image_tool is not None:
                self.response_tools.append(request.image_tool.model_dump(exclude_none=True))
            self.response_tool_choice = request.options.tool_choice
            if request.options.parallel_tool_calls is not None:
                self.response_parallel_tool_calls = request.options.parallel_tool_calls
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
                elif isinstance(block, Image):
                    text_blocks.append({"type": "input_image", "image_url": image_source(block.source), "detail": block.detail})
                elif isinstance(block, (Compaction, GeneratedImage)):
                    items.append(output_item(block, block.id))
                elif isinstance(block, ToolCall):
                    items.append({"type": "function_call", "call_id": block.call_id, "name": block.name, "arguments": block.arguments})
                elif isinstance(block, ToolResult):
                    items.append({"type": "function_call_output", "call_id": block.call_id, "output": block.content})
                elif isinstance(block, Reasoning):
                    if message.role != "assistant":
                        raise GatewayError("unsupported_feature")
                    items.append(output_item(block, block.id))
                elif isinstance(block, Opaque):
                    if block.provider != "codex_oauth" or block.payload.get("type") not in {
                        "additional_tools", "custom_tool_call", "custom_tool_call_output", "function_call"}:
                        raise GatewayError("unsupported_feature")
                    if text_blocks:
                        items.append({"role": message.role, "content": text_blocks})
                        text_blocks = []
                    items.append(additional_tools_item(block.payload) if block.payload.get("type") == "additional_tools"
                                 else native_tool_item(block.payload).payload)
                else:
                    raise GatewayError("unsupported_feature")
            if text_blocks:
                value = {"role": message.role, "content": text_blocks}
                if message.phase is not None:
                    value["phase"] = message.phase
                items.append(value)
        body = {"model": model, "input": items, "stream": True, "store": False}
        if request.instructions is not None:
            body["instructions"] = request.instructions
        if request.continuation is not None:
            body["previous_response_id"] = request.continuation
        native_tools = any(isinstance(block, Opaque) and block.payload.get("type") == "additional_tools"
                           for message in request.messages for block in message.blocks)
        if native_tools:
            body["tool_choice"] = request.options.tool_choice
        if request.tools:
            body["tools"] = [dict(type="function", **tool.model_dump(exclude_none=True)) for tool in request.tools]
            body["tool_choice"] = request.options.tool_choice
        if request.image_tool is not None:
            body.setdefault("tools", []).append(request.image_tool.model_dump(exclude_none=True))
            body["tool_choice"] = request.options.tool_choice
        if request.options.reasoning is not None:
            body["reasoning"] = request.options.reasoning.model_dump(exclude_none=True)
        if request.options.include:
            body["include"] = list(request.options.include)
        if request.options.text is not None:
            body["text"] = TextOptions.model_validate(request.options.text).model_dump(exclude_none=True)
        if request.options.prompt_cache_key is not None:
            body["prompt_cache_key"] = request.options.prompt_cache_key
        if request.client_metadata is not None:
            body["client_metadata"] = dict(request.client_metadata)
        if request.service_tier is not None:
            body["service_tier"] = request.service_tier
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
                "parallel_tool_calls": self.response_parallel_tool_calls,
                "tool_choice": self.response_tool_choice,
                "tools": [dict(tool) for tool in self.response_tools], "usage": None}

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
        if event.kind == "error":
            if self.collector.terminal:
                return []
            code = event.error_code if isinstance(event.error_code, str) and event.error_code in SAFE_CODES else "internal_error"
            response = self._base("failed")
            response["error"] = {"code": code, "message": code}
            self.collector.terminal = True
            self.collector.failed = True
            return [self._event("response.failed", response=response)]
        if event.kind == "started":
            self.response_id = event.response_id or self.response_id
            self.collector = EventCollector(self.response_id, self.model)
        self.collector.feed(event)
        if event.kind == "image_partial":
            return [self._event("response.image_generation_call.partial_image", item_id=event.item_id,
                output_index=event.index, partial_image_index=event.summary_index,
                partial_image_b64=event.block.result)]
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
            index, block = self.collector.blocks[event.item_id]
            values = {"item_id": event.item_id, "output_index": index, "delta": event.delta}
            if event.kind == "text_delta":
                values["content_index"] = 0
            kind = "response.output_text.delta" if event.kind == "text_delta" else "response.function_call_arguments.delta"
            if isinstance(block, Opaque) and block.tool_input_field == "input":
                kind = "response.custom_tool_call_input.delta"
            return [self._event(kind, **values)]
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
            elif isinstance(block, Opaque) and block.tool_input_field:
                field = block.tool_input_field
                kind = "response.custom_tool_call_input.done" if field == "input" else "response.function_call_arguments.done"
                frames.append(self._event(kind, item_id=event.item_id, output_index=index, **{field: block.payload[field]}))
            frames.append(self._event("response.output_item.done", output_index=index, item=output_item(block, event.item_id)))
            return frames
        if event.kind == "finished":
            response = self.encode_result(self.collector.result())
            response["output"] = [output_item(block, key) for key, (_, block) in sorted(self.collector.blocks.items(), key=lambda pair: pair[1][0])]
            return [self._event("response.incomplete" if event.finish_reason == "length" else "response.completed", response=response)]
        return []


def image_source(value):
    """Validate without fetching user URLs (the provider handles image inputs)."""
    from urllib.parse import urlsplit
    if not isinstance(value, str) or not value or len(value) > 16_777_216:
        raise GatewayError("invalid_request")
    if value.startswith("data:image/") and ";base64," in value:
        import base64
        try:
            header, payload = value.split(",", 1)
            if header not in {"data:image/png;base64", "data:image/jpeg;base64", "data:image/webp;base64", "data:image/gif;base64"}:
                raise ValueError()
            if not base64.b64decode(payload, validate=True):
                raise ValueError()
        except ValueError:
            raise GatewayError("invalid_request") from None
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise GatewayError("invalid_request") from None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or any(ord(c) < 33 for c in value):
        raise GatewayError("invalid_request")
    return value
