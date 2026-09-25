import json
import time
from uuid import uuid4

from ..contracts import InferenceRequest, Message, Text, ToolCall, ToolResult, Tool, Reasoning, GenerationOptions
from ..errors import GatewayError


def usage_dict(usage):
    if usage is None:
        return None
    return {"prompt_tokens": usage.input_tokens, "completion_tokens": usage.output_tokens,
            "total_tokens": usage.input_tokens+usage.output_tokens,
            "prompt_tokens_details": {"cached_tokens": usage.cached_read},
            "completion_tokens_details": {"reasoning_tokens": usage.reasoning}}


def sse(value):
    return ("data: "+json.dumps(value, ensure_ascii=False, separators=(",", ":"))+"\n\n").encode()


class OpenAIChatCodec:
    def __init__(self, response_id=None, model="", *, include_usage=False):
        self.response_id, self.model = response_id or "chatcmpl-"+uuid4().hex, model
        self.include_usage = include_usage
        self.created = int(time.time())
        self.tool_indices = {}

    def decode(self, body, options):
        allowed = {"model", "messages", "tools", "tool_choice", "max_tokens", "max_completion_tokens",
                   "temperature", "top_p", "stop", "stream", "stream_options", "n", "parallel_tool_calls"}
        if not isinstance(body, dict) or set(body)-allowed or body.get("n", 1) != 1:
            raise GatewayError("unsupported_feature")
        try:
            if "max_tokens" in body and "max_completion_tokens" in body:
                raise ValueError()
            messages = []
            for message in body["messages"]:
                if set(message)-{"role", "content", "tool_calls", "tool_call_id"}:
                    raise GatewayError("unsupported_feature")
                role, content = message["role"], message.get("content")
                blocks = []
                if role == "tool":
                    blocks.append(ToolResult(call_id=message["tool_call_id"], content=content))
                elif content is not None:
                    if isinstance(content, str):
                        blocks.append(Text(text=content))
                    elif isinstance(content, list):
                        for part in content:
                            if not isinstance(part, dict) or set(part) != {"type", "text"} or part["type"] != "text":
                                raise GatewayError("unsupported_feature")
                            blocks.append(Text(text=part["text"]))
                    else:
                        raise ValueError()
                for call in message.get("tool_calls", []):
                    if call["type"] != "function" or set(call)-{"id", "type", "function"}:
                        raise GatewayError("unsupported_feature")
                    function = call["function"]
                    if set(function)-{"name", "arguments"}:
                        raise GatewayError("unsupported_feature")
                    blocks.append(ToolCall(call_id=call["id"], name=function["name"], arguments=function["arguments"]))
                messages.append(Message(role=role, blocks=tuple(blocks)))
            tools = []
            for item in body.get("tools", []):
                if item["type"] != "function" or set(item)-{"type", "function"}:
                    raise GatewayError("unsupported_feature")
                tools.append(Tool(**item["function"]))
            stop = body.get("stop") or ()
            if isinstance(stop, str):
                stop = (stop,)
            stream_options = body.get("stream_options") or {}
            if set(stream_options)-{"include_usage"}:
                raise GatewayError("unsupported_feature")
            self.include_usage = stream_options.get("include_usage", False)
            result = InferenceRequest(model=body["model"], messages=tuple(messages), tools=tuple(tools),
                stream=body.get("stream", False), options=GenerationOptions(
                    max_output_tokens=body.get("max_completion_tokens", body.get("max_tokens")),
                    temperature=body.get("temperature"), top_p=body.get("top_p"), stop=stop,
                    tool_choice=body.get("tool_choice", "auto"), parallel_tool_calls=body.get("parallel_tool_calls")))
            self.model = result.model
            return result
        except GatewayError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError):
            raise GatewayError("invalid_request") from None

    def upstream_body(self, request, model):
        from .responses_options import reject_native_options
        reject_native_options(request)
        messages = []
        if request.continuation:
            raise GatewayError("unsupported_feature")
        if request.instructions:
            messages.append({"role": "system", "content": request.instructions})
        for message in request.messages:
            result = {"role": message.role, "content": None}
            text, calls = [], []
            for block in message.blocks:
                if isinstance(block, Text):
                    text.append(block.text)
                elif isinstance(block, ToolCall):
                    calls.append({"id": block.call_id, "type": "function", "function": {
                        "name": block.name, "arguments": block.arguments}})
                elif isinstance(block, ToolResult):
                    if len(message.blocks) != 1:
                        raise GatewayError("unsupported_feature")
                    result.update(role="tool", tool_call_id=block.call_id, content=block.content)
                else:
                    raise GatewayError("unsupported_feature")
            if text:
                result["content"] = "".join(text)
            if calls:
                result["tool_calls"] = calls
            messages.append(result)
        body = {"model": model, "messages": messages, "stream": True, "stream_options": {"include_usage": True}}
        if request.tools:
            body["tools"] = [{"type": "function", "function": tool.model_dump(exclude_none=True)} for tool in request.tools]
            body["tool_choice"] = request.options.tool_choice
        option_map = {"max_output_tokens": "max_completion_tokens", "temperature": "temperature", "top_p": "top_p",
                      "parallel_tool_calls": "parallel_tool_calls"}
        for source, target in option_map.items():
            value = getattr(request.options, source)
            if value is not None:
                body[target] = value
        if request.options.stop:
            body["stop"] = list(request.options.stop)
        return body

    def encode_result(self, result):
        message = {"role": "assistant", "content": None}
        text, calls = [], []
        for block in result.blocks:
            if isinstance(block, Reasoning):
                # Responses summaries/signatures are not Chat message content.
                continue
            if isinstance(block, Text):
                text.append(block.text)
            elif isinstance(block, ToolCall):
                calls.append({"id": block.call_id, "type": "function", "function": {"name": block.name, "arguments": block.arguments}})
            else:
                raise GatewayError("unsupported_feature")
        if text:
            message["content"] = "".join(text)
        if calls:
            message["tool_calls"] = calls
        value = {"id": result.id, "object": "chat.completion", "created": self.created, "model": result.model,
                 "choices": [{"index": 0, "message": message, "finish_reason": result.finish_reason}]}
        if result.usage is not None:
            value["usage"] = usage_dict(result.usage)
        return value

    def _chunk(self, delta, finish=None):
        return {"id": self.response_id, "object": "chat.completion.chunk", "created": self.created, "model": self.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    def encode_event(self, event):
        if event.kind == "started":
            return [sse(self._chunk({"role": "assistant", "content": ""}))]
        if event.kind == "text_delta":
            return [sse(self._chunk({"content": event.delta}))]
        if event.kind == "block_started" and isinstance(event.block, ToolCall):
            index = len(self.tool_indices)
            self.tool_indices[event.item_id] = index
            return [sse(self._chunk({"tool_calls": [{"index": index, "id": event.block.call_id, "type": "function",
                "function": {"name": event.block.name, "arguments": event.block.arguments}}]}))]
        if event.kind == "tool_delta":
            return [sse(self._chunk({"tool_calls": [{"index": self.tool_indices[event.item_id],
                                                   "function": {"arguments": event.delta}}]}))]
        if event.kind == "finished":
            frames = [sse(self._chunk({}, event.finish_reason))]
            if self.include_usage and event.usage is not None:
                usage = self._chunk({})
                usage.update(choices=[], usage=usage_dict(event.usage))
                frames.append(sse(usage))
            return frames+[b"data: [DONE]\n\n"]
        if event.kind == "error":
            return [sse(GatewayError(event.error_code or "upstream_error", 502).to_dict())]
        return []
