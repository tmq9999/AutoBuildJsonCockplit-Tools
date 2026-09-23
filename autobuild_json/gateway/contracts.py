from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .metering.records import Usage


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Text(StrictRecord):
    type: Literal["text"] = "text"
    text: str = Field(repr=False)


class Image(StrictRecord):
    type: Literal["image"] = "image"
    source: str = Field(repr=False)
    media_type: str


class ToolCall(StrictRecord):
    type: Literal["tool_call"] = "tool_call"
    call_id: str
    name: str
    arguments: str = Field(default="", repr=False)


class ToolResult(StrictRecord):
    type: Literal["tool_result"] = "tool_result"
    call_id: str
    content: str = Field(repr=False)
    is_error: bool = False


class Opaque(StrictRecord):
    type: Literal["opaque"] = "opaque"
    provider: str
    payload: dict = Field(repr=False)


Block = Annotated[Text | Image | ToolCall | ToolResult | Opaque, Field(discriminator="type")]


class Message(StrictRecord):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    blocks: tuple[Block, ...] = Field(default=(), repr=False)


class Tool(StrictRecord):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", repr=False)
    parameters: dict = Field(default_factory=dict, repr=False)


class GenerationOptions(StrictRecord):
    max_output_tokens: int | None = Field(default=None, strict=True, gt=0, le=1_000_000)
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    top_p: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    stop: tuple[str, ...] = ()
    tool_choice: Literal["auto", "none", "required"] = "auto"
    parallel_tool_calls: bool | None = None


class InferenceRequest(StrictRecord):
    model: str = Field(min_length=1, max_length=200)
    messages: tuple[Message, ...] = Field(default=(), repr=False)
    instructions: str | None = Field(default=None, repr=False)
    tools: tuple[Tool, ...] = Field(default=(), max_length=128, repr=False)
    options: GenerationOptions = Field(default_factory=GenerationOptions)
    stream: bool = False
    continuation: str | None = None

    @property
    def required_capabilities(self):
        capabilities = {"text"}
        blocks = [block for message in self.messages for block in message.blocks]
        if self.tools or any(isinstance(b, (ToolCall, ToolResult)) for b in blocks):
            capabilities.add("tools")
        if any(isinstance(b, Image) for b in blocks):
            capabilities.add("vision")
        if self.continuation:
            capabilities.add("continuation")
        return frozenset(capabilities)

    def validate_provider(self, adapter):
        for message in self.messages:
            for block in message.blocks:
                if isinstance(block, Opaque) and block.provider != adapter:
                    raise ValueError("unsupported_feature")


FinishReason = Literal["stop", "length", "tool_calls", "content_filter"]


class InferenceEvent(StrictRecord):
    kind: Literal["started", "block_started", "text_delta", "tool_delta", "block_finished", "usage", "finished", "error"]
    item_id: str | None = None
    index: int = Field(default=0, ge=0)
    delta: str = Field(default="", repr=False)
    block: Block | None = Field(default=None, repr=False)
    finish_reason: FinishReason | None = None
    usage: Usage | None = None
    error_code: str | None = None
    response_id: str | None = Field(default=None, repr=False)


class InferenceResult(StrictRecord):
    id: str
    model: str
    blocks: tuple[Block, ...] = Field(repr=False)
    finish_reason: FinishReason
    usage: Usage | None = None
