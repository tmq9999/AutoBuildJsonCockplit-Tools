MESSAGES = {
    "INVALID_CREDENTIALS": "Email or password was rejected",
    "ACCOUNT_DEACTIVATED": "Account is deactivated",
    "MFA_ERROR": "TOTP verification failed",
    "EMAIL_OTP_REQUIRED": "Email verification is required",
    "PHONE_VERIFY": "Phone number verify",
    "PROXY_ERROR": "Proxy connection failed",
    "NETWORK_ERROR": "Network request failed",
    "TIMEOUT": "Account deadline exceeded",
    "RATE_LIMITED": "Rate limited; try again later",
    "AUTH_BLOCKED": "Authentication blocked by provider",
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
    def __init__(self, code: str, stage: str = "processing"):
        self.code = code if code in MESSAGES else "UNEXPECTED_ERROR"
        self.stage = stage
        super().__init__(MESSAGES[self.code])

    @property
    def reason(self):
        return MESSAGES[self.code]
