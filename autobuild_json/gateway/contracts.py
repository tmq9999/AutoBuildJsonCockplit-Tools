from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .metering.records import Usage


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Text(StrictRecord):
    type: Literal["text"] = "text"
    text: str = Field(repr=False)
    phase: Literal["commentary", "final_answer"] | None = None


class Image(StrictRecord):
    type: Literal["image"] = "image"
    source: str = Field(repr=False)
    media_type: str
    detail: Literal["auto", "low", "high", "original"] = "auto"


class Compaction(StrictRecord):
    type: Literal["compaction"] = "compaction"
    id: str = Field(min_length=1, max_length=512)
    encrypted_content: str = Field(min_length=1, max_length=4_194_304, repr=False)


class GeneratedImage(StrictRecord):
    type: Literal["generated_image"] = "generated_image"
    id: str = Field(min_length=1, max_length=512)
    result: str | None = Field(default=None, max_length=33_554_432, repr=False)
    status: Literal["in_progress", "generating", "completed", "failed"] = "in_progress"
    revised_prompt: str | None = Field(default=None, max_length=32768, repr=False)
    output_format: Literal["png", "jpeg", "webp"] | None = None
    size: str | None = None
    background: str | None = None
    quality: str | None = None


class ImageGenerationTool(StrictRecord):
    type: Literal["image_generation"] = "image_generation"
    model: str | None = Field(default=None, min_length=1, max_length=200)
    action: Literal["auto", "generate", "edit"] = "auto"
    quality: Literal["auto", "low", "medium", "high"] | None = None
    size: Literal["auto", "1024x1024", "1536x1024", "1024x1536"] | None = None
    background: Literal["auto", "transparent", "opaque"] | None = None
    output_format: Literal["png", "jpeg", "webp"] | None = None
    output_compression: int | None = Field(default=None, strict=True, ge=0, le=100)
    partial_images: int | None = Field(default=None, strict=True, ge=0, le=3)
    moderation: Literal["auto", "low"] | None = None
    input_fidelity: Literal["low", "high"] | None = None
    input_image_mask: dict | None = Field(default=None, repr=False)


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


class ThinkingOptions(StrictRecord):
    """Keep the original Messages intent for native Anthropic forwarding."""
    type: Literal["enabled", "disabled", "adaptive"]
    budget_tokens: int | None = Field(default=None, strict=True, ge=1024, le=1_000_000)

    @model_validator(mode="after")
    def valid_budget(self):
        if (self.type == "enabled") != (self.budget_tokens is not None):
            raise ValueError("invalid_thinking_budget")
        return self


class Opaque(StrictRecord):
    type: Literal["opaque"] = "opaque"
    provider: str
    payload: dict = Field(repr=False)

    @property
    def tool_input_field(self):
        if self.provider == "codex_oauth":
            return {"custom_tool_call": "input", "function_call": "arguments"}.get(self.payload.get("type"))
        return None


Block = Annotated[Text | Image | ToolCall | ToolResult | Reasoning | Compaction | GeneratedImage | Opaque, Field(discriminator="type")]


class Message(StrictRecord):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    blocks: tuple[Block, ...] = Field(default=(), repr=False)
    phase: Literal["commentary", "final_answer"] | None = None


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
    thinking: ThinkingOptions | None = None
    thinking_effort: Literal["low", "medium", "high", "max"] | None = None
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
    operation: Literal["responses", "compact", "websocket"] = "responses"
    image_tool: ImageGenerationTool | None = None
    # Codex sends these request-scoped routing hints with native Responses
    # calls.  They are deliberately kept separate from generation options so
    # other provider adapters can reject the Codex-only opaque fields instead
    # of silently forwarding them.
    client_metadata: dict[str, str] | None = Field(default=None, max_length=32, repr=False)
    service_tier: str | None = Field(default=None, min_length=1, max_length=64)

    @property
    def required_capabilities(self):
        capabilities = {"text"}
        blocks = [block for message in self.messages for block in message.blocks]
        if self.tools or any(isinstance(b, (ToolCall, ToolResult)) or
            (isinstance(b, Opaque) and b.provider == "codex_oauth" and b.payload.get("type") in {
                "additional_tools", "custom_tool_call", "custom_tool_call_output", "function_call"}) for b in blocks):
            capabilities.add("tools")
        if any(isinstance(b, Image) or (isinstance(b, Opaque) and b.provider == "codex_oauth"
                and b.payload.get("type") == "custom_tool_call_output"
                and isinstance(b.payload.get("output"), list)
                and any(isinstance(part, dict) and part.get("type") == "input_image"
                        for part in b.payload["output"])) for b in blocks):
            capabilities.add("vision")
        if self.continuation:
            capabilities.add("continuation")
        if self.operation != "responses":
            capabilities.add(self.operation)
        if self.image_tool:
            capabilities.add("images")
        return frozenset(capabilities)

    def validate_provider(self, adapter):
        if (self.operation != "responses" or self.image_tool) and adapter != "codex_oauth":
            raise ValueError("unsupported_feature")
        for message in self.messages:
            for block in message.blocks:
                if isinstance(block, Opaque) and block.provider != adapter:
                    raise ValueError("unsupported_feature")

    @model_validator(mode="after")
    def validate_request_metadata(self):
        if self.client_metadata is not None:
            if any(not isinstance(key, str) or not 1 <= len(key) <= 128
                   or not isinstance(value, str) or len(value) > 8192
                   for key, value in self.client_metadata.items()):
                raise ValueError("invalid_client_metadata")
        return self


FinishReason = Literal["stop", "length", "tool_calls", "content_filter"]


class InferenceEvent(StrictRecord):
    kind: Literal["started", "block_started", "text_delta", "tool_delta", "reasoning_summary_started",
                  "reasoning_summary_delta", "reasoning_summary_finished", "image_partial", "block_finished", "usage", "finished", "error"]
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
