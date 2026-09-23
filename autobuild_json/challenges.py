from urllib.parse import urlsplit

from .errors import FlowError
from .diagnostics import request_details, safe_path


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
    stage = "sentinel" if urlsplit(url).hostname == "sentinel.openai.com" else "authentication"
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    location = response.headers.get("Location", "")
    page = payload.get("page")
    page_type = page.get("type") if isinstance(page, dict) else None
    details = {**request_details(None, url), "http_status": response.status_code,
        "page_type":page_type, "redirect_path":safe_path(location), "continuation_path":safe_path(continuation(payload))}
    challenge = detect_challenge(payload, url) or detect_challenge({}, location)
    if challenge:
        if detect_challenge({"page":{"type":page_type}}, "") == challenge:
            details["evidence"] = "page_type"
        elif detect_challenge({}, url) == challenge:
            details["evidence"] = "current_url"
        elif detect_challenge({}, continuation(payload)) == challenge:
            details["evidence"] = "continuation"
        elif detect_challenge({}, location) == challenge:
            details["evidence"] = "redirect"
        else:
            details["evidence"] = "provider_code"
        raise FlowError(challenge, stage, details=details)
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else error
    details["provider_code"] = code
    if code == "account_deactivated":
        raise FlowError("ACCOUNT_DEACTIVATED", stage, details=details)
    if code == "invalid_state" or response.status_code == 409:
        raise FlowError("INVALID_STATE", stage, details=details)
    if response.status_code == 401:
        raise FlowError("MFA_ERROR" if "/mfa/" in url else "INVALID_CREDENTIALS", stage, details=details)
    if response.status_code == 429:
        raise FlowError("RATE_LIMITED", stage, details=details)
    if response.status_code >= 400:
        raise FlowError("AUTH_BLOCKED" if response.status_code == 403 else "HTTP_ERROR", stage, details=details)
    # A successful Sentinel metadata response is input to the pinned client,
    # not proof of a user-facing verification wall. In particular, `dx` alone
    # must not preempt upstream's token construction/fallback. Actual challenge
    # pages/redirects and HTTP rejection statuses above still stop the attempt.
    return payload
