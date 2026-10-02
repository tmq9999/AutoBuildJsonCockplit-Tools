import json

from ..contracts import InferenceResult, Text, ToolCall, Reasoning, GeneratedImage, Compaction, Opaque, REASONING_BYTE_LIMIT, REASONING_PART_LIMIT


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
        self.reasoning_parts = {}
        self.reasoning_bytes = 0
        self.reasoning_encrypted_bytes = 0
        self.reasoning_part_count = 0

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
            if isinstance(event.block, Opaque) and event.block.tool_input_field:
                self.tool_count += 1
                self.tool_bytes += len(event.block.payload[event.block.tool_input_field].encode())
            if isinstance(event.block, Reasoning):
                if event.block.id != event.item_id:
                    raise ValueError("invalid_reasoning_id")
                self.reasoning_parts[event.item_id] = {i for i in range(len(event.block.summary))}
                self.reasoning_bytes += sum(len(part.encode()) for part in event.block.summary)
                self.reasoning_part_count += len(event.block.summary)
                self.reasoning_encrypted_bytes += len((event.block.encrypted_content or "").encode())
        elif event.kind in {"reasoning_summary_started", "reasoning_summary_delta", "reasoning_summary_finished"}:
            if event.item_id not in self.blocks or event.item_id in self.closed:
                raise ValueError("invalid_block")
            index, block = self.blocks[event.item_id]
            if not isinstance(block, Reasoning):
                raise ValueError("invalid_reasoning_event")
            parts, finished = list(block.summary), self.reasoning_parts[event.item_id]
            part = event.summary_index
            if event.kind == "reasoning_summary_started":
                if part != len(parts) or len(parts) >= REASONING_PART_LIMIT or len(finished) != len(parts):
                    raise ValueError("invalid_reasoning_part")
                parts.append(event.delta)
                self.reasoning_bytes += len(event.delta.encode())
                self.reasoning_part_count += 1
            elif part >= len(parts) or part in finished:
                raise ValueError("invalid_reasoning_part")
            elif event.kind == "reasoning_summary_delta":
                parts[part] += event.delta
                self.reasoning_bytes += len(event.delta.encode())
            else:
                if parts[part] != event.delta:
                    raise ValueError("reasoning_summary_mismatch")
                finished.add(part)
            self.blocks[event.item_id] = (index, block.model_copy(update={"summary": tuple(parts)}))
        elif event.kind in {"text_delta", "tool_delta", "block_finished"}:
            if event.item_id not in self.blocks or event.item_id in self.closed:
                raise ValueError("invalid_block")
            index, block = self.blocks[event.item_id]
            if event.kind == "text_delta" and isinstance(block, Text):
                block = block.model_copy(update={"text": block.text + event.delta})
            elif event.kind == "tool_delta" and isinstance(block, ToolCall):
                self.tool_bytes += len(event.delta.encode())
                block = block.model_copy(update={"arguments": block.arguments + event.delta})
            elif event.kind == "tool_delta" and isinstance(block, Opaque) and block.tool_input_field:
                self.tool_bytes += len(event.delta.encode())
                field = block.tool_input_field
                block = block.model_copy(update={"payload": {**block.payload, field: block.payload[field] + event.delta}})
            elif event.kind == "block_finished":
                if isinstance(block, Opaque) and block.tool_input_field:
                    if block.tool_input_field == "arguments" and not isinstance(json.loads(block.payload["arguments"]), dict):
                        raise ValueError("invalid_tool_arguments")
                    if event.block is not None:
                        if not isinstance(event.block, Opaque) or event.block.provider != block.provider:
                            raise ValueError("invalid_tool_item")
                        completed_fields = {"status", "metadata", "internal_chat_message_metadata_passthrough"}
                        original = {k: v for k, v in block.payload.items() if k not in completed_fields}
                        final = {k: v for k, v in event.block.payload.items() if k not in completed_fields}
                        if original != final:
                            raise ValueError("tool_item_mismatch")
                        block = event.block
                if isinstance(block, Text) and event.block is not None:
                    if not isinstance(event.block, Text) or event.block.text != block.text:
                        raise ValueError("invalid_text_item")
                    if block.phase is not None and event.block.phase != block.phase:
                        raise ValueError("message_phase_mismatch")
                    block = event.block
                if isinstance(block, GeneratedImage) and event.block is not None:
                    if not isinstance(event.block, GeneratedImage) or event.block.id != block.id:
                        raise ValueError("invalid_image")
                    block = event.block
                if isinstance(block, ToolCall) and not isinstance(json.loads(block.arguments), dict):
                    raise ValueError("invalid_tool_arguments")
                if isinstance(block, Reasoning):
                    if len(self.reasoning_parts[event.item_id]) != len(block.summary):
                        raise ValueError("incomplete_reasoning_summary")
                    if event.block is not None:
                        final = event.block
                        if not isinstance(final, Reasoning) or final.id != block.id:
                            raise ValueError("invalid_reasoning_id")
                        if block.summary and final.summary != block.summary:
                            raise ValueError("reasoning_summary_mismatch")
                        self.reasoning_bytes += sum(len(part.encode()) for part in final.summary) - sum(len(part.encode()) for part in block.summary)
                        self.reasoning_part_count += len(final.summary) - len(block.summary)
                        self.reasoning_encrypted_bytes += len((final.encrypted_content or "").encode()) - len((block.encrypted_content or "").encode())
                        block = final
                elif not isinstance(block, (Text, ToolCall, GeneratedImage, Compaction, Opaque)):
                    raise ValueError("invalid_delta")
                self.closed.add(event.item_id)
            else:
                raise ValueError("invalid_delta")
            self.blocks[event.item_id] = (index, block)
        elif event.kind == "image_partial":
            if (event.item_id not in self.blocks or event.item_id in self.closed
                    or not isinstance(self.blocks[event.item_id][1], GeneratedImage)
                    or not isinstance(event.block, GeneratedImage)):
                raise ValueError("invalid_image")
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
        if (self.reasoning_bytes > REASONING_BYTE_LIMIT or self.reasoning_encrypted_bytes > REASONING_BYTE_LIMIT
                or self.reasoning_part_count > REASONING_PART_LIMIT):
            raise ValueError("reasoning_limit_exceeded")

    def result(self):
        if not self.terminal or self.failed:
            raise ValueError("incomplete_output")
        return InferenceResult(id=self.id, model=self.model, blocks=tuple(b for _, b in sorted(self.blocks.values())),
                               finish_reason=self.finish_reason, usage=self.usage)
