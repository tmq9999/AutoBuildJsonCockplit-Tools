import json
from collections import deque
from uuid import uuid4

from ..contracts import InferenceRequest, Message, Text, ToolCall, ToolResult, Tool, Reasoning, GenerationOptions
from ..errors import GatewayError
from .common import EventCollector


def validate_query(query, streaming):
    if "key" in query:
        raise ValueError("query_credentials_forbidden")
    if streaming and query == {"alt": "sse"}:
        return "sse"
    if not streaming and not query:
        return "json"
    raise ValueError("unsupported_query")


def gemini_usage(usage):
    return {"promptTokenCount": usage.input_tokens, "candidatesTokenCount": usage.output_tokens-usage.reasoning,
            "thoughtsTokenCount": usage.reasoning, "cachedContentTokenCount": usage.cached_read,
            "totalTokenCount": usage.input_tokens+usage.output_tokens}


class GeminiCodec:
    def __init__(self, response_id=None, model=""):
        self.response_id, self.model = response_id or "gen_"+uuid4().hex, model
        self.collector = None

    def decode(self, body, options):
        if not isinstance(body, dict) or set(body)-{"contents", "systemInstruction", "tools", "generationConfig"}:
            raise GatewayError("unsupported_feature")
        try:
            messages, names, call_names = [], {}, {}
            for item in body.get("contents", []):
                if set(item)-{"role", "parts"}:
                    raise GatewayError("unsupported_feature")
                blocks = []
                for part in item["parts"]:
                    if set(part) == {"text"}:
                        blocks.append(Text(text=part["text"]))
                    elif set(part) == {"functionCall"}:
                        call = part["functionCall"]
                        if set(call)-{"name", "args", "id"}:
                            raise GatewayError("unsupported_feature")
                        identity = call.get("id") or "call_"+uuid4().hex
                        if identity in call_names:
                            raise ValueError()
                        call_names[identity] = call["name"]
                        names.setdefault(call["name"], deque()).append(identity)
                        blocks.append(ToolCall(call_id=identity, name=call["name"], arguments=json.dumps(call["args"])))
                    elif set(part) == {"functionResponse"}:
                        result = part["functionResponse"]
                        if set(result)-{"id", "name", "response"}:
                            raise GatewayError("unsupported_feature")
                        name = result["name"]
                        queue = names.get(name)
                        identity = result.get("id")
                        if identity is None:
                            if not queue:
                                raise ValueError()
                            identity = queue.popleft()
                        elif call_names.get(identity) != name or not queue:
                            raise ValueError()
                        else:
                            queue.remove(identity)
                        call_names.pop(identity)
                        blocks.append(ToolResult(call_id=identity, content=json.dumps(result["response"])))
                    else:
                        raise GatewayError("unsupported_feature")
                role = {"model": "assistant", "user": "user"}[item.get("role", "user")]
                messages.append(Message(role=role, blocks=blocks))
            system = body.get("systemInstruction", {})
            if set(system)-{"parts", "role"} or any(set(p) != {"text"} for p in system.get("parts", [])):
                raise GatewayError("unsupported_feature")
            tools = []
            for group in body.get("tools", []):
                if set(group) != {"functionDeclarations"}:
                    raise GatewayError("unsupported_feature")
                tools.extend(Tool(**tool) for tool in group["functionDeclarations"])
            config = body.get("generationConfig", {})
            if set(config)-{"maxOutputTokens", "temperature", "topP", "stopSequences", "candidateCount"} or config.get("candidateCount", 1) != 1:
                raise GatewayError("unsupported_feature")
            request = InferenceRequest(model=options["model"], messages=messages, tools=tools, stream=options.get("stream", False),
                instructions="\n".join(p["text"] for p in system.get("parts", [])) or None,
                options=GenerationOptions(max_output_tokens=config.get("maxOutputTokens"), temperature=config.get("temperature"),
                                          top_p=config.get("topP"), stop=config.get("stopSequences", ())))
            self.model = request.model
            return request
        except GatewayError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError):
            raise GatewayError("invalid_request") from None

    def upstream_body(self, request, model):
        from .responses_options import reject_native_options
        reject_native_options(request, strict_tools=True)
        names, contents, system = {}, [], []
        if request.instructions:
            system.append(request.instructions)
        for message in request.messages:
            if message.role in {"system", "developer"}:
                if any(not isinstance(b, Text) for b in message.blocks):
                    raise GatewayError("unsupported_feature")
                system.extend(b.text for b in message.blocks)
                continue
            parts = []
            for block in message.blocks:
                if isinstance(block, Text):
                    parts.append({"text": block.text})
                elif isinstance(block, ToolCall):
                    names[block.call_id] = block.name
                    parts.append({"functionCall": {"name": block.name, "args": json.loads(block.arguments)}})
                elif isinstance(block, ToolResult):
                    if block.call_id not in names:
                        raise GatewayError("unsupported_feature")
                    try:
                        response = json.loads(block.content)
                    except ValueError:
                        response = {"result": block.content}
                    if not isinstance(response, dict):
                        response = {"result": response}
                    parts.append({"functionResponse": {"name": names[block.call_id], "response": response}})
                else:
                    raise GatewayError("unsupported_feature")
            contents.append({"role": "model" if message.role == "assistant" else "user", "parts": parts})
        body = {"contents": contents}
        if system:
            body["systemInstruction"] = {"parts": [{"text": "\n".join(system)}]}
        if request.tools:
            body["tools"] = [{"functionDeclarations": [tool.model_dump(exclude_none=True) for tool in request.tools]}]
        config = {}
        for source, target in (("max_output_tokens", "maxOutputTokens"), ("temperature", "temperature"), ("top_p", "topP")):
            if getattr(request.options, source) is not None:
                config[target] = getattr(request.options, source)
        if request.options.stop:
            config["stopSequences"] = list(request.options.stop)
        if request.options.tool_choice != "auto" or request.options.parallel_tool_calls is not None or request.continuation:
            raise GatewayError("unsupported_feature")
        if config:
            body["generationConfig"] = config
        return body

    def encode_result(self, result):
        parts = []
        for block in result.blocks:
            if isinstance(block, Reasoning):
                # No compatible signature/summary representation is supported.
                continue
            if isinstance(block, Text):
                parts.append({"text": block.text})
            elif isinstance(block, ToolCall):
                parts.append({"functionCall": {"name": block.name, "args": json.loads(block.arguments)}})
            else:
                raise GatewayError("unsupported_feature")
        response = {"candidates": [{"index": 0, "content": {"role": "model", "parts": parts},
                    "finishReason": {"stop": "STOP", "tool_calls": "STOP", "length": "MAX_TOKENS", "content_filter": "SAFETY"}[result.finish_reason]}],
                    "modelVersion": result.model}
        if result.usage:
            response["usageMetadata"] = gemini_usage(result.usage)
        return response

    def encode_event(self, event):
        if event.kind == "started":
            self.collector = EventCollector(self.response_id, self.model)
        self.collector.feed(event)
        output = None
        if event.kind == "text_delta":
            output = {"candidates": [{"index": 0, "content": {"role": "model", "parts": [{"text": event.delta}]}}]}
        elif event.kind == "block_finished":
            _, block = self.collector.blocks[event.item_id]
            if isinstance(block, ToolCall):
                output = {"candidates": [{"index": 0, "content": {"role": "model", "parts": [
                    {"functionCall": {"name": block.name, "args": json.loads(block.arguments)}}]}}]}
        elif event.kind == "finished":
            output = self.encode_result(self.collector.result())
            output["candidates"][0]["content"]["parts"] = []
        elif event.kind == "error":
            output = {"error": {"code": 502, "status": "INTERNAL", "message": event.error_code}}
        return [("data: "+json.dumps(output, separators=(",", ":"))+"\n\n").encode()] if output is not None else []
