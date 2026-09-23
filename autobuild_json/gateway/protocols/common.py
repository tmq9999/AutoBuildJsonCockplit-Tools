import json

from ..contracts import InferenceResult, Text, ToolCall


class EventCollector:
    def __init__(self, response_id, model):
        self.id, self.model = response_id, model
        self.started = self.terminal = False
        self.blocks = {}
        self.closed = set()
        self.indexes = set()
        self.usage = None
        self.finish_reason = None
        self.tool_bytes = 0
        self.tool_count = 0
        self.failed = False

    def feed(self, event):
        if self.terminal:
            raise ValueError("event_after_terminal")
        if event.kind == "started":
            if self.started:
                raise ValueError("duplicate_start")
            self.started = True
            return
        if not self.started:
            raise ValueError("missing_start")
        if event.kind == "block_started":
            if event.item_id is None or event.item_id in self.blocks or event.index in self.indexes or event.block is None:
                raise ValueError("invalid_block")
            self.blocks[event.item_id] = (event.index, event.block)
            self.indexes.add(event.index)
            if isinstance(event.block, ToolCall):
                self.tool_count += 1
                self.tool_bytes += len(event.block.arguments.encode())
        elif event.kind in {"text_delta", "tool_delta", "block_finished"}:
            if event.item_id not in self.blocks or event.item_id in self.closed:
                raise ValueError("invalid_block")
            index, block = self.blocks[event.item_id]
            if event.kind == "text_delta" and isinstance(block, Text):
                block = block.model_copy(update={"text": block.text + event.delta})
            elif event.kind == "tool_delta" and isinstance(block, ToolCall):
                self.tool_bytes += len(event.delta.encode())
                block = block.model_copy(update={"arguments": block.arguments + event.delta})
            elif event.kind == "block_finished":
                if isinstance(block, ToolCall) and not isinstance(json.loads(block.arguments), dict):
                    raise ValueError("invalid_tool_arguments")
                self.closed.add(event.item_id)
            else:
                raise ValueError("invalid_delta")
            self.blocks[event.item_id] = (index, block)
        elif event.kind == "usage":
            if event.usage is None:
                raise ValueError("invalid_usage")
            self.usage = event.usage
        elif event.kind == "finished":
            if self.closed != set(self.blocks) or event.finish_reason is None:
                raise ValueError("incomplete_output")
            self.terminal = True
            self.finish_reason = event.finish_reason
            if event.usage is not None:
                self.usage = event.usage
        elif event.kind == "error":
            self.terminal = self.failed = True
        if self.tool_bytes > 1_048_576 or self.tool_count > 128:
            raise ValueError("tool_limit_exceeded")

    def result(self):
        if not self.terminal or self.failed:
            raise ValueError("incomplete_output")
        return InferenceResult(id=self.id, model=self.model, blocks=tuple(b for _, b in sorted(self.blocks.values())),
                               finish_reason=self.finish_reason, usage=self.usage)
