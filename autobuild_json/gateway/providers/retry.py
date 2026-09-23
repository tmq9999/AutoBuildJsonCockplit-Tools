"""Bounded Retry-After handling; never retain provider bodies or raw headers."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import ceil
import re

_DAY = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_TIME = r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
_HTTP_DATE = re.compile(
    rf"(?:{_DAY}, [0-9]{{2}} {_MONTH} [0-9]{{4}} {_TIME} GMT|"
    rf"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), [0-9]{{2}}-{_MONTH}-[0-9]{{2}} {_TIME} GMT|"
    rf"{_DAY} {_MONTH} (?:[0-9]{{2}}| [0-9]) {_TIME} [0-9]{{4}})"
)


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> int | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    value = value.strip(" \t")
    if not value or any(ord(char) < 32 or ord(char) >= 127 for char in value):
        return None
    if value.isdecimal():
        return min(int(value), 86400)
    if not _HTTP_DATE.fullmatch(value):
        return None
    try:
        date = parsedate_to_datetime(value)
        # The obsolete asctime HTTP-date form has no explicit timezone (UTC).
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        seconds = ceil((date - (now or datetime.now(timezone.utc))).total_seconds())
        return max(0, min(seconds, 86400))
    except (ValueError, TypeError, OverflowError):
        return None
