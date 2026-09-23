"""Decimal-string management wire format; public inference usage stays numeric."""

import re
from starlette.responses import JSONResponse


def exact_input(value):
    if isinstance(value, list):
        return [exact_input(v) for v in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key.endswith("_micro") and isinstance(item, str):
                if not re.fullmatch(r"\d{1,38}", item):
                    raise ValueError("invalid_micro_units")
                item = int(item)
            result[key] = exact_input(item)
        return result
    return value


def exact_output(value):
    if isinstance(value, list):
        return [exact_output(v) for v in value]
    if isinstance(value, dict):
        return {
            k: str(v)
            if (k.endswith("_micro") or k in {"held", "spent", "hold"}) and type(v) is int
            else exact_output(v)
            for k, v in value.items()
        }
    return value


class ExactJSONResponse(JSONResponse):
    def render(self, content):
        return super().render(exact_output(content))
