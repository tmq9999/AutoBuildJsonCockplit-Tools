from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


REASONING_PART_LIMIT = 128
REASONING_BYTE_LIMIT = 1_048_576


class Reasoning(StrictRecord):
    """Provider-published summaries plus an opaque Responses replay payload.

    Never holds or requests private reasoning text; encrypted_content is not
    decoded and must not be mapped into another protocol's text fields.
    """

    type: Literal["reasoning"] = "reasoning"
    id: str = Field(min_length=1, max_length=512)
    summary: tuple[str, ...] = Field(default=(), max_length=REASONING_PART_LIMIT, repr=False)
    encrypted_content: str | None = Field(default=None, max_length=REASONING_BYTE_LIMIT, repr=False)
    status: Literal["in_progress", "completed", "incomplete"] | None = None

    @model_validator(mode="after")
    def bounded_payload(self):
        if sum(len(part.encode()) for part in self.summary) > REASONING_BYTE_LIMIT:
            raise ValueError("reasoning_limit_exceeded")
        if self.encrypted_content is not None and len(self.encrypted_content.encode()) > REASONING_BYTE_LIMIT:
            raise ValueError("reasoning_limit_exceeded")
        return self


class ReasoningOptions(StrictRecord):
    """Portable Responses reasoning controls.

    Codex accepts these controls but not the generic sampling/output caps.
    Keeping a typed subset prevents arbitrary upstream fields from becoming a
    header/body injection surface while allowing the official effort and
    summary/context modes.
    """

    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] | None = None
    summary: Literal["auto", "concise", "detailed"] | None = None
    context: Literal["current_turn", "all_turns"] | None = None


class Opaque(StrictRecord):
    type: Literal["opaque"] = "opaque"
    provider: str
    payload: dict = Field(repr=False)


Block = Annotated[Text | Image | ToolCall | ToolResult | Reasoning | Opaque, Field(discriminator="type")]


class Message(StrictRecord):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    blocks: tuple[Block, ...] = Field(default=(), repr=False)


class Tool(StrictRecord):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", repr=False)
    parameters: dict = Field(default_factory=dict, repr=False)
    strict: bool | None = Field(default=None, strict=True)


class GenerationOptions(StrictRecord):
    max_output_tokens: int | None = Field(default=None, strict=True, gt=0, le=1_000_000)
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    top_p: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    stop: tuple[str, ...] = ()
    tool_choice: Literal["auto", "none", "required"] = "auto"
    parallel_tool_calls: bool | None = None
    reasoning: ReasoningOptions | None = None
    include: tuple[Literal["reasoning.encrypted_content"], ...] = Field(default=(), max_length=1)
    text: dict | None = Field(default=None, repr=False)
    prompt_cache_key: str | None = Field(default=None, min_length=1, max_length=512, repr=False)


class InferenceRequest(StrictRecord):
    # Client namespace prefix (64) plus a canonical model/alias (200).
    model: str = Field(min_length=1, max_length=264)
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
    kind: Literal["started", "block_started", "text_delta", "tool_delta", "reasoning_summary_started",
                  "reasoning_summary_delta", "reasoning_summary_finished", "block_finished", "usage", "finished", "error"]
    item_id: str | None = None
    index: int = Field(default=0, ge=0)
    summary_index: int = Field(default=0, strict=True, ge=0, lt=REASONING_PART_LIMIT)
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
