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


def _rate(value):
    if type(value) is not int or value < 0 or value >= 10**38:
        raise ValueError("invalid_usage")


def weighted_usage_micro(usage, input_micro, output_micro, cache_read_micro=None, cache_write_micro=None):
    """Charge canonical usage using exclusive cache buckets."""
    if usage is None:
        raise ValueError("invalid_usage")
    _rate(input_micro)
    _rate(output_micro)
    cr = input_micro if cache_read_micro is None else cache_read_micro
    cw = input_micro if cache_write_micro is None else cache_write_micro
    _rate(cr)
    _rate(cw)
    if any(type(v) is not int or v < 0 for v in
           (usage.input_tokens, usage.output_tokens, usage.cached_read, usage.cached_write)):
        raise ValueError("invalid_usage")
    if usage.cached_read + usage.cached_write > usage.input_tokens:
        raise ValueError("invalid_usage")
    uncached = usage.input_tokens - usage.cached_read - usage.cached_write
    total = (uncached * input_micro + usage.cached_read * cr + usage.cached_write * cw
             + usage.output_tokens * output_micro)
    if total >= 10**38:
        raise ValueError("quota_overflow")
    return total


def hold_micro(bounds, input_micro, output_micro, cache_read_micro=None, cache_write_micro=None):
    """Reserve enough for any cache allocation within the input bound."""
    _rate(input_micro)
    _rate(output_micro)
    cr = input_micro if cache_read_micro is None else cache_read_micro
    cw = input_micro if cache_write_micro is None else cache_write_micro
    _rate(cr)
    _rate(cw)
    if type(bounds.input_tokens) is not int or bounds.input_tokens < 0 or type(bounds.output_tokens) is not int or bounds.output_tokens < 0:
        raise ValueError("invalid_usage")
    total = bounds.input_tokens * max(input_micro, cr, cw) + bounds.output_tokens * output_micro
    if total >= 10**38:
        raise ValueError("quota_overflow")
    return total
