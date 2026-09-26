"""Validated, deliberately bounded native Responses request options."""
from typing import Literal

from pydantic import Field, field_validator

from ..contracts import StrictRecord
from ..errors import GatewayError


class TextOptions(StrictRecord):
    verbosity: Literal["low", "medium", "high"] | None = None
    format: dict | None = Field(default=None, repr=False)

    @field_validator("format")
    @classmethod
    def validate_format(cls, value):
        if value is None:
            return value
        kind = value.get("type")
        if kind in {"text", "json_object"} and set(value) == {"type"}:
            return value
        if kind != "json_schema" or set(value) - {"type", "name", "schema", "description", "strict"}:
            raise ValueError("invalid_text_format")
        name = value.get("name")
        if (not isinstance(name, str) or not 1 <= len(name) <= 64
                or not all(c.isascii() and (c.isalnum() or c in "_-") for c in name)
                or not isinstance(value.get("schema"), dict)
                or ("description" in value and not isinstance(value["description"], str))
                or ("strict" in value and value["strict"] is not None and type(value["strict"]) is not bool)):
            raise ValueError("invalid_text_format")
        return value


def reject_native_options(request, *, strict_tools=False, allow_effort=False):
    """Do not silently lose native options on an unrelated wire API."""
    opts = request.options
    reasoning = opts.reasoning
    unsupported_reasoning = reasoning is not None and (not allow_effort or reasoning.context is not None
        or reasoning.summary not in {None, 'auto'})
    if (unsupported_reasoning or opts.thinking is not None or opts.thinking_effort is not None
            or opts.include or opts.text is not None or opts.prompt_cache_key is not None
            or (strict_tools and any(tool.strict is not None for tool in request.tools))):
        raise GatewayError("unsupported_feature")
