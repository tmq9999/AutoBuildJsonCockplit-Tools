from urllib.parse import urlsplit

from .errors import FlowError


def continuation(payload):
    if not isinstance(payload, dict):
        return ""
    page = payload.get("page")
    nested = page.get("payload", {}) if isinstance(page, dict) else {}
    value = payload.get("continue_url") or (nested.get("continue_url") if isinstance(nested, dict) else "")
    return value if isinstance(value, str) else ""


def detect_challenge(payload, url):
    payload = payload if isinstance(payload, dict) else {}
    error = payload.get("error")
    error_code = error.get("code") if isinstance(error, dict) else error
    if isinstance(error_code, str) and error_code.lower() in {
        "phone_verification", "phone_verification_required", "phone_number_verification_required",
        "verify_phone", "phone_number_required", "sms_verification_required",
    }:
        return "PHONE_VERIFY"
    page = payload.get("page")
    kind = str(page.get("type", "")).lower() if isinstance(page, dict) else ""
    path = urlsplit(url).path.lower()
    next_path = urlsplit(continuation(payload)).path.lower()
    for candidate in (kind, path, next_path):
        if any(marker in candidate for marker in ("phone_verification", "phone-verification", "verify_phone", "add-phone", "sms-challenge")):
            return "PHONE_VERIFY"
        if "email_otp" in candidate or "email-verification" in candidate:
            return "EMAIL_OTP_REQUIRED"
        if "captcha" in candidate or "turnstile" in candidate:
            return "ACTION_REQUIRED"
    return None


def inspect_response(response, url):
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    challenge = detect_challenge(payload, url) or detect_challenge({}, response.headers.get("Location", ""))
    if challenge:
        raise FlowError(challenge, "authentication")
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else error
    if code == "account_deactivated":
        raise FlowError("ACCOUNT_DEACTIVATED", "authentication")
    if code == "invalid_state" or response.status_code == 409:
        raise FlowError("INVALID_STATE", "authentication")
    if response.status_code == 401:
        raise FlowError("MFA_ERROR" if "/mfa/" in url else "INVALID_CREDENTIALS", "authentication")
    if response.status_code == 429:
        raise FlowError("RATE_LIMITED", "authentication")
    if response.status_code >= 400:
        raise FlowError("AUTH_BLOCKED", "authentication")
    if urlsplit(url).hostname == "sentinel.openai.com":
        turnstile = payload.get("turnstile")
        if isinstance(turnstile, dict) and (turnstile.get("required") or turnstile.get("dx")):
            raise FlowError("ACTION_REQUIRED", "sentinel")
    return payload
