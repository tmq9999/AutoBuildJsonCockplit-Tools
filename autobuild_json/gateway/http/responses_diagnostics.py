"""Bounded diagnostic labels, never exception messages or request values."""
import json
import logging

from pydantic import ValidationError

logger = logging.getLogger(__name__)

PATHS = frozenset({"/v1/responses", "/backend-api/codex/responses",
                   "/v1/responses/compact", "/backend-api/codex/responses/compact"})
ORIGINS = {
    "autobuild_json.gateway.protocols.openai_responses": frozenset({
        "decode", "native_tool_item", "additional_tools_item", "reasoning_item", "message_phase",
        "caller_value", "custom_tool_output_part", "chat_messages_input", "image_source", "upstream_body"}),
    "autobuild_json.gateway.protocols.openai_chat": frozenset({"decode"}),
    "autobuild_json.gateway.protocols.responses_options": frozenset({"validate_format"}),
    "autobuild_json.gateway.http.auth": frozenset({"gateway_session", "client_secret"}),
    "autobuild_json.gateway.http.app": frozenset({"responses"}),
    "autobuild_json.gateway.http.codex_routes": frozenset({"compact"}),
    "autobuild_json.gateway.engine": frozenset({"meta", "prepare", "session_digest", "collect", "events"}),
}
REASONS = frozenset({
    "invalid_message_phase", "invalid_tool_caller", "invalid_tool_output", "invalid_tool_input",
    "invalid_tool_name", "invalid_tool_namespace", "invalid_tool_async", "invalid_tool_metadata",
    "invalid_tool_call_id", "invalid_tool_id", "invalid_tool_status", "tool_limit_exceeded",
    "invalid_reasoning_metadata", "reasoning_metadata_limit_exceeded", "invalid_reasoning_summary",
    "invalid_reasoning_content", "invalid_text_format",
})
FIELDS = frozenset({
    "model", "messages", "instructions", "tools", "image_tool", "stream", "continuation",
    "client_metadata", "service_tier", "options", "max_output_tokens", "temperature", "top_p",
    "tool_choice", "parallel_tool_calls", "reasoning", "include", "text", "prompt_cache_key",
    "role", "blocks", "phase", "type", "id", "call_id", "name", "arguments", "content",
    "summary", "encrypted_content", "status", "source", "media_type", "detail", "parameters",
    "description", "strict", "format", "verbosity", "schema", "effort", "context", "provider",
    "payload", "output_format", "quality", "size", "background", "input_image_mask",
})
VALIDATION_TYPES = frozenset({
    "missing", "extra_forbidden", "string_type", "string_too_long", "string_too_short",
    "string_pattern_mismatch", "int_type", "int_parsing", "float_type", "float_parsing",
    "bool_type", "bool_parsing", "list_type", "tuple_type", "dict_type", "model_type",
    "literal_error", "value_error", "greater_than", "greater_than_equal", "less_than",
    "less_than_equal", "too_long", "too_short", "finite_number", "union_tag_invalid",
    "union_tag_not_found", "none_required",
})


def log_responses_rejection(path, error):
    if path not in PATHS or error.code not in {"invalid_request", "unsupported_feature"}:
        return
    origin, line, reason, validation = "other", 0, "unspecified", "none"
    current, seen = error, set()
    # Suppressed exception contexts still retain the original decoder failure.
    # Never format exceptions/tracebacks: they can contain secrets and prompts.
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        trace = current.__traceback__
        for _ in range(64):
            if trace is None:
                break
            module = trace.tb_frame.f_globals.get("__name__", "")
            function = trace.tb_frame.f_code.co_name
            if function in ORIGINS.get(module, ()):
                origin, line = module.rsplit(".", 1)[-1] + "." + function, trace.tb_lineno
            trace = trace.tb_next
        if isinstance(current, ValidationError):
            reason = "validation_error"
            labels = []
            for detail in current.errors(include_input=False, include_context=False, include_url=False)[:6]:
                location = ".".join("*" if type(part) is int else part if part in FIELDS else "other"
                                    for part in detail["loc"][:6]) or "root"
                kind = detail["type"] if detail["type"] in VALIDATION_TYPES else "other"
                labels.append(location + ":" + kind)
            validation = ",".join(labels) or "none"
        elif isinstance(current, json.JSONDecodeError):
            reason = "invalid_json"
        elif type(current) is ValueError:
            label = current.args[0] if len(current.args) == 1 else None
            reason = label if isinstance(label, str) and label in REASONS else "invalid_value"
        elif isinstance(current, KeyError):
            reason = "missing_field"
        elif isinstance(current, TypeError):
            reason = "invalid_type"
        elif isinstance(current, AttributeError):
            reason = "invalid_shape"
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    logger.warning("responses request rejected code=%s stage=%s status=%s origin=%s line=%s reason=%s validation=%s",
                   error.code, error.stage, error.status, origin, line, reason, validation)
