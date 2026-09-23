import base64
import binascii
import re
import uuid

from .errors import FlowError
from .models import AccountInput, ParseReport, RejectedLine

MAX_BYTES = 5 * 1024 * 1024
MAX_LINES = 10_000


def valid_email(value: str) -> bool:
    return len(value) <= 254 and re.fullmatch(r"[^\s@|<>]+@[^\s@|<>]+\.[^\s@|<>]+", value) is not None


def parse_accounts(text: str) -> ParseReport:
    try:
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise FlowError("INVALID_INPUT", "input")
    except UnicodeError:
        raise FlowError("INVALID_INPUT", "input") from None
    lines = text.removeprefix("\ufeff").splitlines()
    if len(lines) > MAX_LINES:
        raise FlowError("INVALID_INPUT", "input")
    accounts, rejected, duplicates, seen = [], [], [], set()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        fields = line.split("|")
        code, reason = "INVALID_FIELDS", "Expected three non-empty fields"
        if len(fields) == 3 and all(part.strip() for part in fields):
            email, password, secret = fields
            email = email.strip()
            secret = secret.strip().replace(" ", "").replace("-", "").upper()
            if not valid_email(email):
                code, reason = "INVALID_EMAIL", "Invalid email format"
            else:
                try:
                    decoded = base64.b32decode(secret + "=" * (-len(secret) % 8))
                    if not decoded:
                        raise ValueError()
                except (ValueError, binascii.Error):
                    code, reason = "INVALID_SECRET", "Invalid TOTP secret"
                else:
                    if email.casefold() in seen:
                        duplicates.append(number)
                    seen.add(email.casefold())
                    accounts.append(AccountInput(str(uuid.uuid4()), number, email, password, secret))
                    continue
        rejected.append(RejectedLine(number, code, reason))
    return ParseReport(accounts, rejected, duplicates)
