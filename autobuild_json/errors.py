from .diagnostics import sanitize_details


MESSAGES = {
    "INVALID_CREDENTIALS": "Email or password was rejected",
    "ACCOUNT_DEACTIVATED": "Account is deactivated",
    "MFA_ERROR": "TOTP verification failed",
    "EMAIL_OTP_REQUIRED": "Email verification is required",
    "PHONE_VERIFY": "Phone number verify",
    "PROXY_ERROR": "Proxy connection failed",
    "NETWORK_ERROR": "Network request failed",
    "REQUEST_TIMEOUT": "HTTP request timed out",
    "HTTP_ERROR": "Provider returned an HTTP error",
    "AUTH_RESPONSE_ERROR": "Authentication response could not be processed",
    "OAUTH_PARSE_ERROR": "Could not read account or workspace choices from the provider response",
    "ACCOUNT_SELECTION_REQUIRED": "More than one login session is available",
    "TIMEOUT": "Account deadline exceeded",
    "RATE_LIMITED": "Rate limited; try again later",
    "AUTH_BLOCKED": "Authentication blocked by provider",
    "AUTH_BOOTSTRAP_ERROR": "Unexpected providers, CSRF or sign-in response",
    "ACTION_REQUIRED": "Additional verification requires manual action",
    "INVALID_STATE": "Invalid, expired or already consumed OAuth state",
    "OAUTH_DENIED": "OAuth authorization was denied",
    "WORKSPACE_SELECTION_REQUIRED": "A unique workspace could not be selected",
    "TOKEN_EXCHANGE_ERROR": "Authorization code exchange failed",
    "TOKEN_EXCHANGE_UNCERTAIN": "Exchange outcome unknown; start a new OAuth session",
    "INVALID_TOKEN_RESPONSE": "Token response or identity validation failed",
    "ACCOUNT_MISMATCH": "Authenticated email does not match the requested account",
    "CONFIGURATION_ERROR": "Invalid configuration or CheckLive dependency is not ready",
    "INVALID_INPUT": "Input is invalid or exceeds the configured limit",
    "CANCELLED": "Cancelled by operator",
    "INTERRUPTED": "Interrupted by application restart",
    "STORAGE_ERROR": "Unable to persist results; batch stopped",
    "NOT_FOUND": "Result not found",
    "BATCH_CONFLICT": "Another batch is active",
    "UNEXPECTED_ERROR": "Unexpected processing error",
}


class FlowError(Exception):
    def __init__(self, code: str, stage: str = "processing", *, details=None):
        self.code = code if code in MESSAGES else "UNEXPECTED_ERROR"
        self.stage = stage
        self.details = sanitize_details(details)
        super().__init__(MESSAGES[self.code])

    def with_stage(self, stage):
        return FlowError(self.code, stage, details=self.details)

    @property
    def reason(self):
        return MESSAGES[self.code]
