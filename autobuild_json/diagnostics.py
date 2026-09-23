"""Allowlisted diagnostics only: never persist arbitrary provider text or URLs."""
from urllib.parse import urlsplit

HOSTS = {"chatgpt.com", "auth.openai.com", "sentinel.openai.com"}
PATHS = {
    "/", "/api/auth/providers", "/api/auth/csrf", "/api/auth/signin/openai", "/api/auth/callback/openai",
    "/api/accounts/authorize", "/api/accounts/authorize/continue", "/api/accounts/password/verify",
    "/api/accounts/mfa/issue_challenge", "/api/accounts/mfa/verify", "/api/accounts/client_auth_session_dump",
    "/api/accounts/session/select", "/api/accounts/workspace/select", "/oauth/authorize", "/oauth/token",
    "/api/oauth/oauth2/auth", "/.well-known/jwks.json", "/log-in", "/log-in/password", "/email-verification",
    "/mfa-challenge/:id", "/add-phone", "/phone-verification", "/choose-an-account", "/consent", "/workspace",
    "/sign-in-with-chatgpt/codex/consent", "/backend-api/sentinel/req",
}
ENUMS = {
    "host": HOSTS, "endpoint": PATHS, "redirect_path": PATHS, "continuation_path": PATHS,
    "method": {"GET", "POST", "HEAD"}, "selection_kind": {"account", "workspace"},
    "evidence": {"page_type", "redirect", "current_url", "continuation", "provider_code"},
    "page_type": {"login_password", "mfa_totp", "email_otp", "email_otp_verification", "email_verification",
                  "phone_verification", "captcha", "turnstile"},
    "provider_code": {"account_deactivated", "invalid_state", "invalid_credentials", "invalid_password",
                      "invalid_otp", "mfa_invalid_code", "rate_limit_exceeded", "access_denied", "invalid_request",
                      "invalid_grant", "phone_verification", "phone_verification_required"},
    "exception_type": {"Timeout", "ConnectionError", "DNSError", "SSLError", "ProxyError", "RequestException",
                       "RequestsError", "OSError", "TypeError", "ValueError", "KeyError", "AttributeError"},
}
LIMITS = {"http_status": (100, 599), "curl_code": (0, 999), "candidate_count": (0, 10000),
          "timeout_ms": (0, 600000)}


def sanitize_details(details):
    if not isinstance(details, dict):
        return {}
    safe = {}
    for key, value in details.items():
        if key in ENUMS and isinstance(value, str) and value in ENUMS[key]:
            safe[key] = value
        elif key in LIMITS and type(value) is int and LIMITS[key][0] <= value <= LIMITS[key][1]:
            safe[key] = value
    return safe


def safe_path(url):
    try:
        path = urlsplit(url).path
    except (ValueError, TypeError, AttributeError):
        return None
    if path in PATHS:
        return path
    if path.startswith("/mfa-challenge/"):
        return "/mfa-challenge/:id"
    return None


def request_details(method, url):
    try:
        host = urlsplit(url).hostname
    except (ValueError, TypeError, AttributeError):
        host = None
    return sanitize_details({"host": host, "endpoint": safe_path(url), "method": method})
