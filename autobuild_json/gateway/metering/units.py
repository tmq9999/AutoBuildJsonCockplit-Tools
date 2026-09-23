from decimal import Decimal, InvalidOperation, localcontext


def coefficient_micro(value):
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError()
        number = Decimal(value)
        if not number.is_finite() or number < 0 or number >= Decimal("1e32"):
            raise ValueError()
        with localcontext() as context:
            context.prec = 80
            scaled = number * 1_000_000
            if scaled != scaled.to_integral_value():
                raise ValueError()
        return int(scaled)
    except (ValueError, InvalidOperation):
        raise ValueError("invalid_coefficient") from None


def weighted_micro(input_tokens, output_tokens, input_micro, output_micro):
    if any(type(v) is not int or v < 0 for v in (input_tokens, output_tokens, input_micro, output_micro)):
        raise ValueError("invalid_usage")
    total = input_tokens * input_micro + output_tokens * output_micro
    if total >= 10**38:
        raise ValueError("quota_overflow")
    return total
