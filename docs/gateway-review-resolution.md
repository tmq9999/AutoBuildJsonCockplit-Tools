# Review resolution — API gateway

Independent read-only whole-branch review covered `d78dfed..bff7c14` and communicated CRUD updates. Initial verdict was not ready: ten Important findings, no Critical. The implementer reproduced the findings and fixed them in one regression pass; no second review or live-provider approval is implied.

| Finding | Resolution and regression |
|---|---|
| Local adapter rejection held quota | Adapter body/capability and egress preflight precedes dispatch; `test_adapter_validation_failure_is_provably_not_dispatched`. |
| Waiting refresh worker replayed uncertain grant | Recheck uncertain/reauth state after lease; `test_refresh_rechecks_uncertain_health_after_winning_lease`. |
| Cancelled curl thread outlived lease | Shield worker and await close before releasing; `test_refresh_cancellation_waits_for_http_thread_close`. |
| Legacy heartbeat cancelled completed task | Acquire/use/exit in live owner task; `test_legacy_lease_loss_cancels_live_processor`. |
| Anthropic initial tool arguments lost | Emit initial JSON delta; `test_anthropic_encoder_preserves_initial_tool_arguments`. |
| Predispatch money hold orphaned | Persist request link and reconcile released requests too; both money recovery tests in `test_review_regressions.py`. |
| Nonstream/preparation ignored disconnect | Request-lifetime ASGI receive watcher; `test_nonstream_disconnect_cancels_generation`. |
| Bound violation route stayed active | Quarantine binding by actual input/output counts, even zero coefficients; `test_usage_bound_violation_quarantines_route_even_with_zero_coefficient`. |
| Configured auth header ignored | Shared auth-header mapping; `test_configured_upstream_auth_mode_used_for_inference`. |
| Provider RPM/concurrency missing | Shared PostgreSQL admission rows with provider/credential locks; concurrency and post-completion RPM tests. |

Verification after fixes: 446 Python tests passed, including real PostgreSQL, SDK HTTP tests and 40 cross-protocol JSON/stream text cases. 12 Node tests, old OAuth browser and new gateway browser E2E passed. Ruff/compile/build passed. One upstream google-genai deprecation warning remains.

Remaining limits: no live OAuth-inference/Kiot/provider credentials tested; no full provider API/CLI parity, media generation or payment system; proxy-side DNS enforcement requires trusted egress; resale rights are not conferred by this code. UI was locally inspected by the implementer, not pixel-audited by the code reviewer. Documentation Minor (stale UI approval status) was corrected as part of the handoff.

