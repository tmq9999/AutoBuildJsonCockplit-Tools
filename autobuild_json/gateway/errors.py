"""No raw provider message, URL, credential or input is a public error field."""
SAFE_CODES = frozenset({
    "internal_error", "invalid_api_key", "permission_denied", "version_conflict", "not_found",
    "invalid_request", "unsupported_feature", "quota_exceeded", "rate_limited", "concurrency_limit",
    "upstream_unavailable", "upstream_error", "proxy_not_ready", "proxy_error", "deadline_exceeded",
    "storage_unavailable", "usage_pending", "invalid_usage", "invalid_state", "duplicate_request",
    "invalid_keyring", "secret_unavailable", "reauth_required", "refresh_uncertain", "egress_denied",
    "payload_mismatch", "budget_exceeded", "catalog_incomplete", "catalog_snapshot_stale",
    "catalog_limit_exceeded", "catalog_conflict", "probe_budget_required",
    "kiot_key_invalid", "kiot_unavailable", "kiot_allocation_uncertain",
    "operation_conflict", "claim_lost", "operation_cancelled", "operation_expired",
    "codex_usage_unavailable", "codex_credits_unavailable", "codex_credits_stale", "codex_no_credits",
    "codex_reset_uncertain", "codex_refresh_failed", "codex_reset_applied", "codex_reset_not_applied",
    "image_accounts_required",
    "model_not_supported", "provider_rejected", "unsupported_reasoning_effort",
})
SAFE_STAGES = frozenset({"auth", "policy", "storage", "quota", "proxy", "upstream", "stream", "request", "refresh"})


class GatewayError(Exception):
    def __init__(self, code, status=400, stage="request", retry_after=None, *, upstream_status=None):
        self.code = code if code in SAFE_CODES else "internal_error"
        self.status = status if isinstance(status, int) and 400 <= status <= 599 else 500
        self.stage = stage if stage in SAFE_STAGES else "request"
        self.retry_after = retry_after if type(retry_after) is int and 0 <= retry_after <= 86400 else None
        self.upstream_status = (upstream_status if type(upstream_status) is int
                                and 100 <= upstream_status <= 599 else None)
        super().__init__(self.code)

    def to_dict(self):
        message = ("Codex does not accept this reasoning effort. Use none, minimal, low, medium, high, xhigh or max."
                   if self.code == 'unsupported_reasoning_effort' else self.code)
        return {"error": {"code": self.code, "message": message, "stage": self.stage}}


class UpstreamRejected(GatewayError):
    def __init__(self, status, retry_after=None, code=None):
        super().__init__(code or ("rate_limited" if status == 429 else "upstream_error"),
                         429 if status == 429 else 502, "upstream", retry_after if status == 429 else None)
        self.upstream_status = status
        if self.code == 'unsupported_reasoning_effort':
            self.status = 400
        self.safe_retry = status == 429
