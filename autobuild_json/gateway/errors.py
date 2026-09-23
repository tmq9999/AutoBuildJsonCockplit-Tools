"""No raw provider message, URL, credential or input is a public error field."""
SAFE_CODES = frozenset({
    "internal_error", "invalid_api_key", "permission_denied", "version_conflict", "not_found",
    "invalid_request", "unsupported_feature", "quota_exceeded", "rate_limited", "concurrency_limit",
    "upstream_unavailable", "upstream_error", "proxy_not_ready", "proxy_error", "deadline_exceeded",
    "storage_unavailable", "usage_pending", "invalid_usage", "invalid_state", "duplicate_request",
    "invalid_keyring", "secret_unavailable", "reauth_required", "refresh_uncertain", "egress_denied",
})
SAFE_STAGES = frozenset({"auth", "policy", "storage", "quota", "proxy", "upstream", "stream", "request", "refresh"})


class GatewayError(Exception):
    def __init__(self, code, status=400, stage="request", retry_after=None):
        self.code = code if code in SAFE_CODES else "internal_error"
        self.status = status if isinstance(status, int) and 400 <= status <= 599 else 500
        self.stage = stage if stage in SAFE_STAGES else "request"
        self.retry_after = retry_after if type(retry_after) is int and 0 <= retry_after <= 86400 else None
        super().__init__(self.code)

    def to_dict(self):
        return {"error": {"code": self.code, "message": self.code, "stage": self.stage}}
