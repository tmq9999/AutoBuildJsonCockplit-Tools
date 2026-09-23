import json
from uuid import uuid4

from ..contracts import InferenceRequest, Message, Text, ToolCall, ToolResult, Tool, GenerationOptions
from ..errors import GatewayError

STOP_OUT = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use", "content_filter": "refusal"}


def content_block(block):
    if isinstance(block, Text):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolCall):
        return {"type": "tool_use", "id": block.call_id, "name": block.name, "input": json.loads(block.arguments)}
    if isinstance(block, ToolResult):
        result = {"type": "tool_result", "tool_use_id": block.call_id, "content": block.content}
        if block.is_error:
            result["is_error"] = True
        return result
    raise GatewayError("unsupported_feature")


class AnthropicCodec:
    def __init__(self, response_id=None, model=""):
        self.response_id, self.model = response_id or "msg_"+uuid4().hex, model
        self.indices = {}

    def decode(self, body, options):
        allowed = {"model", "max_tokens", "system", "messages", "tools", "tool_choice", "temperature", "top_p", "stop_sequences", "stream"}
        if not isinstance(body, dict) or set(body)-allowed:
            raise GatewayError("unsupported_feature")
        try:
            messages = []
            for message in body["messages"]:
                if set(message) != {"role", "content"} or message["role"] not in {"user", "assistant"}:
                    raise ValueError()
                content = message["content"]
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                blocks = []
                for item in content:
                    kind = item["type"]
                    if kind == "text" and set(item) == {"type", "text"}:
                        blocks.append(Text(text=item["text"]))
                    elif kind == "tool_use" and set(item) == {"type", "id", "name", "input"}:
                        blocks.append(ToolCall(call_id=item["id"], name=item["name"], arguments=json.dumps(item["input"], separators=(",", ":"))))
                    elif kind == "tool_result" and not set(item)-{"type", "tool_use_id", "content", "is_error"}:
                        blocks.append(ToolResult(call_id=item["tool_use_id"], content=item["content"], is_error=item.get("is_error", False)))
                    else:
                        raise GatewayError("unsupported_feature")
                messages.append(Message(role=message["role"], blocks=blocks))
            system = body.get("system")
            if isinstance(system, list):
                if any(set(item) != {"type", "text"} or item["type"] != "text" for item in system):
                    raise GatewayError("unsupported_feature")
                system = "\n".join(item["text"] for item in system)
            choice = body.get("tool_choice", {"type": "auto"})
            if set(choice)-{"type", "disable_parallel_tool_use"} or choice["type"] not in {"auto", "any", "none"}:
                raise GatewayError("unsupported_feature")
            tools = []
            for item in body.get("tools", []):
                if set(item)-{"name", "description", "input_schema"}:
                    raise GatewayError("unsupported_feature")
                tools.append(Tool(name=item["name"], description=item.get("description", ""), parameters=item["input_schema"]))
            request = InferenceRequest(model=body["model"], messages=messages, instructions=system, tools=tools,
                stream=body.get("stream", False), options=GenerationOptions(max_output_tokens=body["max_tokens"],
                    temperature=body.get("temperature"), top_p=body.get("top_p"), stop=body.get("stop_sequences", ()),
                    tool_choice={"auto": "auto", "any": "required", "none": "none"}[choice["type"]],
                    parallel_tool_calls=not choice["disable_parallel_tool_use"] if "disable_parallel_tool_use" in choice else None))
            self.model = request.model
            return request
        except GatewayError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError):
            raise GatewayError("invalid_request") from None

    def upstream_body(self, request, model):
        messages, system = [], []
        if request.instructions:
            system.append(request.instructions)
        for message in request.messages:
            if message.role in {"system", "developer"}:
                if any(not isinstance(block, Text) for block in message.blocks):
                    raise GatewayError("unsupported_feature")
                system.extend(block.text for block in message.blocks)
            else:
                messages.append({"role": "user" if message.role == "tool" else message.role,
                                 "content": [content_block(block) for block in message.blocks]})
        body = {"model": model, "messages": messages, "max_tokens": request.options.max_output_tokens, "stream": True}
        if system:
            body["system"] = "\n".join(system)
        if request.tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in request.tools]
            body["tool_choice"] = {"type": {"auto": "auto", "required": "any", "none": "none"}[request.options.tool_choice]}
            if request.options.parallel_tool_calls is not None:
                body["tool_choice"]["disable_parallel_tool_use"] = not request.options.parallel_tool_calls
        for key in ("temperature", "top_p"):
            if getattr(request.options, key) is not None:
                body[key] = getattr(request.options, key)
        if request.options.stop:
            body["stop_sequences"] = list(request.options.stop)
        return body

    def encode_result(self, result):
        usage = result.usage
        return {"id": result.id, "type": "message", "role": "assistant", "model": result.model,
                "content": [content_block(b) for b in result.blocks], "stop_reason": STOP_OUT[result.finish_reason],
                "stop_sequence": None, "usage": self._usage(usage) if usage else None}

    @staticmethod
    def _usage(usage):
        return {"input_tokens": usage.input_tokens-usage.cached_read-usage.cached_write, "output_tokens": usage.output_tokens,
                "cache_read_input_tokens": usage.cached_read, "cache_creation_input_tokens": usage.cached_write}

    @staticmethod
    def _event(kind, **data):
        return ("event: "+kind+"\ndata: "+json.dumps({"type": kind, **data}, ensure_ascii=False, separators=(",", ":"))+"\n\n").encode()

    def encode_event(self, event):
        if event.kind == "started":
            return [self._event("message_start", message={"id": self.response_id, "type": "message", "role": "assistant",
                "model": self.model, "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}})]
        if event.kind == "block_started":
            self.indices[event.item_id] = event.index
            block = {"type": "tool_use", "id": event.block.call_id, "name": event.block.name, "input": {}} if isinstance(event.block, ToolCall) else content_block(event.block)
            return [self._event("content_block_start", index=event.index, content_block=block)]
        if event.kind in {"text_delta", "tool_delta"}:
            delta = {"type": "text_delta", "text": event.delta} if event.kind == "text_delta" else {"type": "input_json_delta", "partial_json": event.delta}
            return [self._event("content_block_delta", index=self.indices[event.item_id], delta=delta)]
        if event.kind == "block_finished":
            return [self._event("content_block_stop", index=self.indices[event.item_id])]
        if event.kind == "finished":
            return [self._event("message_delta", delta={"stop_reason": STOP_OUT[event.finish_reason], "stop_sequence": None},
                                usage=self._usage(event.usage) if event.usage else {}), self._event("message_stop")]
        if event.kind == "error":
            return [self._event("error", error={"type": "api_error", "message": event.error_code or "upstream_error"})]
        return []
