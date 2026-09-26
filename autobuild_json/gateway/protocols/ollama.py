from datetime import datetime, timezone
from collections import deque
import json
from uuid import uuid4

from ..contracts import InferenceRequest, Message, Text, ToolCall, ToolResult, Reasoning, GenerationOptions
from ..errors import GatewayError
from .common import EventCollector
from .openai_chat import OpenAIChatCodec


def ndjson_record(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"))+"\n").encode()


class OllamaCodec:
    media_type = "application/x-ndjson"

    def __init__(self, *, generate=False):
        self.generate = generate
        self.response_id, self.model = "ollama_"+uuid4().hex, ""
        self.created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.collector = None

    def decode(self, body, options):
        allowed = {"model", "stream", "options", "prompt", "system"} if self.generate else {"model", "stream", "options", "messages", "tools"}
        if not isinstance(body, dict) or set(body)-allowed:
            raise GatewayError("unsupported_feature")
        try:
            opts = body.get("options", {})
            if set(opts)-{"num_predict", "temperature", "top_p", "stop"}:
                raise GatewayError("unsupported_feature")
            if self.generate:
                request = InferenceRequest(model=body["model"], messages=(Message(role="user", blocks=(Text(text=body["prompt"]),)),),
                    instructions=body.get("system"), stream=body.get("stream", True), options=GenerationOptions(
                        max_output_tokens=opts.get("num_predict"), temperature=opts.get("temperature"), top_p=opts.get("top_p"), stop=opts.get("stop", ())))
            else:
                messages = []
                names = {}
                for original in body["messages"]:
                    message = dict(original)
                    if "tool_calls" in message:
                        message["tool_calls"] = [{"id": call.get("id") or "call_"+uuid4().hex, "type": "function",
                            "function": {"name": call["function"]["name"], "arguments": json.dumps(call["function"]["arguments"])}}
                            for call in message["tool_calls"]]
                        for call in message["tool_calls"]:
                            names.setdefault(call["function"]["name"], deque()).append(call["id"])
                    if message.get("role") == "tool" and "tool_name" in message:
                        queue = names.get(message.pop("tool_name"))
                        if not queue:
                            raise ValueError()
                        message["tool_call_id"] = queue.popleft()
                    messages.append(message)
                request = OpenAIChatCodec().decode({"model": body["model"], "messages": messages, "tools": body.get("tools", []),
                    "stream": body.get("stream", True), "max_tokens": opts.get("num_predict"), "temperature": opts.get("temperature"),
                    "top_p": opts.get("top_p"), "stop": opts.get("stop", [])}, {})
            self.model = request.model
            return request
        except GatewayError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError):
            raise GatewayError("invalid_request") from None

    def upstream_body(self, request, model):
        from .responses_options import reject_native_options
        reject_native_options(request, strict_tools=True)
        if request.continuation or request.options.tool_choice != "auto" or request.options.parallel_tool_calls is not None:
            raise GatewayError("unsupported_feature")
        messages = []
        if request.instructions:
            messages.append({"role": "system", "content": request.instructions})
        names = {}
        for message in request.messages:
            item = {"role": message.role, "content": ""}
            calls = []
            for block in message.blocks:
                if isinstance(block, Text):
                    item["content"] += block.text
                elif isinstance(block, ToolCall):
                    names[block.call_id] = block.name
                    calls.append({"function": {"name": block.name, "arguments": json.loads(block.arguments)}})
                elif isinstance(block, ToolResult):
                    if block.call_id not in names or len(message.blocks) != 1:
                        raise GatewayError("unsupported_feature")
                    item.update(role="tool", content=block.content, tool_name=names[block.call_id])
                else:
                    raise GatewayError("unsupported_feature")
            if calls:
                item["tool_calls"] = calls
            messages.append(item)
        body = {"model": model, "messages": messages, "stream": True}
        opts = {}
        for source, target in (("max_output_tokens", "num_predict"), ("temperature", "temperature"), ("top_p", "top_p")):
            if getattr(request.options, source) is not None:
                opts[target] = getattr(request.options, source)
        if request.options.stop:
            opts["stop"] = list(request.options.stop)
        if opts:
            body["options"] = opts
        if request.tools:
            body["tools"] = [{"type": "function", "function": tool.model_dump(exclude_none=True)} for tool in request.tools]
        return body

    def _base(self, done=False):
        result = {"model": self.model, "created_at": self.created, "done": done}
        result.update({"response": ""} if self.generate else {"message": {"role": "assistant", "content": ""}})
        return result

    def encode_result(self, result):
        output = self._base(True)
        text, calls = "", []
        for block in result.blocks:
            if isinstance(block, Reasoning):
                # Native reasoning replay payloads are Responses-only.
                continue
            if isinstance(block, Text):
                text += block.text
            elif isinstance(block, ToolCall) and not self.generate:
                calls.append({"function": {"name": block.name, "arguments": json.loads(block.arguments)}})
            else:
                raise GatewayError("unsupported_feature")
        if self.generate:
            output["response"] = text
        else:
            output["message"]["content"] = text
            if calls:
                output["message"]["tool_calls"] = calls
        output["done_reason"] = "length" if result.finish_reason == "length" else "stop"
        if result.usage:
            output.update(prompt_eval_count=result.usage.input_tokens, eval_count=result.usage.output_tokens)
        return output

    def encode_event(self, event):
        if event.kind == "started":
            self.collector = EventCollector(self.response_id, self.model)
        self.collector.feed(event)
        if event.kind == "text_delta":
            value = self._base()
            if self.generate:
                value["response"] = event.delta
            else:
                value["message"]["content"] = event.delta
            return [ndjson_record(value)]
        if event.kind == "block_finished":
            _, block = self.collector.blocks[event.item_id]
            if isinstance(block, ToolCall):
                if self.generate:
                    raise GatewayError("unsupported_feature")
                value = self._base()
                value["message"]["tool_calls"] = [{"function": {"name": block.name, "arguments": json.loads(block.arguments)}}]
                return [ndjson_record(value)]
        if event.kind == "finished":
            value = self.encode_result(self.collector.result())
            if self.generate:
                value["response"] = ""
            else:
                value["message"] = {"role": "assistant", "content": ""}
            return [ndjson_record(value)]
        if event.kind == "error":
            return [ndjson_record({"error": event.error_code or "upstream_error"})]
        return []
