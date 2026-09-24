# Codex API Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend this gateway with independently written Codex account operations, account pools and exact customer input/cache/output accounting, following the observable Codex API Service behavior of Cockpit.

**Architecture:** Extend the existing FastAPI services, encrypted credentials, guarded transport and PostgreSQL ledger; do not replace them with a desktop sidecar. Deliver three reviewable slices: accounting (Tasks 1–2), account operations/routing (Tasks 3–8), and approved admin UI/acceptance (Tasks 9–12). Tasks share request, credential and proxy contracts below; run implementation tasks sequentially.

**Tech Stack:** Python 3.10/3.14, FastAPI/Pydantic v2, SQLAlchemy async PostgreSQL/Alembic, pytest, vanilla JavaScript, Node tests, Playwright-core/Chrome, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-25-codex-api-service-design.md`

**Status:** Written-spec approved by user with “duyệt spec, subagent-driven”. This plan still requires user review before implementation. Execution method is already selected; do not ask the user to choose it again.

## Global Constraints

The following requirements are copied from the approved spec:

- “Dịch vụ phải dùng PostgreSQL làm nguồn sự thật cho cấu hình và kế toán.”
- “Chỉ hành vi và giao thức tương thích được tham khảo; không sao chép source, UI, sidecar hay cấu trúc dữ liệu của Cockpit.”
- “Customer quota vẫn do gateway quản lý riêng với quota OpenAI.”
- “Với hạn mức 100.000.000 token, reservation và settlement dùng fixed-point integer micro-token, không dùng float. Cache không bị tính hai lần.”
- “Default `cache_read_rate` và `cache_write_rate` bằng `input_rate`”.
- “Request mới không được đổi account giữa stream.”
- “Không retry timeout, partial stream hoặc request đã có generation.”
- “Admin route yêu cầu private session + CSRF; public route chỉ nhận gateway key.”
- “Không log token, Kiot key, client key, raw upstream body hoặc cookie.”
- “Không bật scheduler tự động trong startup public/admin; worker được khởi động tường minh.”
- “Không chạy reset, refresh quota, migration production hoặc test account thật trong CI; dùng fixture response đã redact.”

Additional execution boundaries retained from the current branch: no real accounts/keys, production migration, reset, service restart, deploy, merge or push. Existing four public protocols and the OAuth Workbench remain compatible. UI retains matte dark-bronze Vietnamese styling. Fixtures use synthetic identities and `provider.invalid`; no copied reference fixtures.

## Review Focus

1. Cache is inclusive for OpenAI/Responses and exclusive for Anthropic; Task 1 tests equal normalized usage, Task 2 checks a 100m key, fractional rates and simultaneous final settlement.
2. Two workers, a double-click, crash or disconnect must not spend two reset credits; Tasks 3/5 test unique durable operation, fencing and an uncertain POST with no automatic replay.
3. Credential proxy overrides must apply identically to refresh, quota and inference; Tasks 4/7 test all four modes plus provider inheritance and profile edits in flight.
4. An exhausted account must not disable every sibling of the Codex provider; Tasks 6/7 test account-only cooldown, one pre-generation retry, continuation isolation and no retry after any stream event.
5. UI unknown/stale quota must not become 100% remaining, and unsupported compact/images/WebSocket must not be marketed as supported; Tasks 8/11 test truthful capabilities, unknown values and no upstream I/O on rejection.

## Resume and reference boundary

- Worktree: `/home/mquangprovip0503/Work/Code/AutoBuildJsonCockplit-Tools/.worktrees/api-gateway`, branch `feat/provider-catalog`, inspection baseline `3f24815`.
- Prior provider-catalog Tasks 1–12 are complete through `fa9cc76`. Do not redispatch them. Its draft v4 was approved by the subsequent user message; implementation Tasks 14/15 remain unfinished and separate. This plan must preserve those backend APIs and record handoff dependencies, not mark unfinished catalog UI complete.
- Reference commit: Cockpit `79870647b7d3403da037cd0064bccb5092822743`. Read `docs/CODEX_API_SERVICE_HANDOFF.md`, `src/services/codexLocalAccessService.ts`, `src/types/codexLocalAccess.ts`, and the quota/reset service behavior for contracts, not copyable implementation. Its desktop startup/takeover, sidecar and image/WebSocket features are not implemented by these tasks.
- `chatgpt.com/backend-api/*` behavior is reference-observed, not a documented stable OpenAI public API or proof of availability for every account. No real reset request is authorized by this plan.

## Shared contracts and resolved ambiguities

### Accounting

Reuse `metering.records.Usage`; it already validates inclusive input and cache subsets. Do not introduce a second incompatible usage dataclass. `uncached = input_tokens - cached_read - cached_write`. Customer rates are stored in integer micro-units; upstream monetary `CostSchedule` remains independent.

Add nullable `cache_read_micro` and `cache_write_micro` to `ModelConfig`, `ModelRate`, `Admission` and `RouteSnapshot`. For Pydantic models these fields are optional; for frozen dataclasses append them after existing defaulted fields with defaults to preserve positional callers. In `Admission`, effective rate fields must be included in the constructor before validation; `Ledger.reserve` reads the four fields from the admission snapshot. Null inherits the effective input rate, including a key override's input rate; explicit zero is free, never treated as missing. Selecting a key/model override replaces the complete rate tuple: its null cache values inherit that override's input, not the model's cache rate. Snapshot all four effective rates at admission. Existing rows backfill both cache rates from their stored input rate without re-rating historical settlements.

Hold = `input_bound * max(input_micro, cache_read_micro, cache_write_micro) + output_bound * output_micro`, since cache allocation is unknown before inference. Settle actual exclusive buckets once. Preserve current excess-bound policy: charge at most held amount, retain actual usage and computed amount, flag excess, quarantine the binding; no silent loss of usage or negative available quota. Pending holds survive failures and restarts.

For key `100000000` tokens, `total_micro = 100000000000000`. With inclusive input 1000, cache-read 600, cache-write 100, output 200 and rates 1 / 0.1 / 1.25 / 3, charge is 1085 tokens and remaining is 99,998,915. With inherited 1 / 1 / 1 / 1, charge is 1200, not 1900.

Raw token usage means token counts, not saved provider response bodies. Protocol codecs retain their own public usage shape; OpenAI counts remain inclusive, Anthropic input remains exclusive. Do not insert weighted quotas into client usage.

### Records, storage and privacy

New immutable Pydantic DTOs belong in `accounts/quota_records.py` and `routing/pool_records.py` with `extra='forbid'`, bounded strings/collections, strict integers, aware timestamps and nullable unknowns. Account IDs, token values and upstream error bodies never enter DTO error text. Quota percentages use finite Decimal values between 0 and 100; serialize as decimal strings in private management responses, not approximate floats. `reset_at` uses aware UTC; UI labels actual `window_seconds`, rather than assuming all primary windows are 5 hours.

- `QuotaWindow(used_percent: Decimal | None, window_seconds: int | None, reset_at: datetime | None)`.
- `CodexQuotaSnapshot(credential_id: UUID, version: int, plan_type: str | None, primary: QuotaWindow | None, secondary: QuotaWindow | None, limit_reached: bool | None, fetched_at: datetime, last_error: str | None)`.
- `ResetCredit(id: str | None, state: Literal['available','redeemed','expired','unknown'], expires_at: datetime | None)` and `ResetCreditsSnapshot(credential_id: UUID, version: int, available_count: int | None, credits: tuple[ResetCredit, ...], fetched_at: datetime)`; max 1000 records, IDs max 200 printable characters.
- `ResetResult(operation_id: UUID, credential_id: UUID, state: Literal['prepared','dispatched','rejected','unknown','succeeded','succeeded_refresh_failed'], result_code: str | None, upstream_status: int | None)`.
- `PoolMember(credential_id: UUID, priority: int = 0, weight: int = 1, backup: bool = False)` with priority 0–10000, weight 1–10000; max 1000 unique members.
- `AccountPoolPolicy(mode: Literal['auto','random','single','priority','weight'], members: tuple[PoolMember, ...], single_credential_id: UUID | None = None, session_affinity: bool = True, reserve_enabled: bool = False, min_primary_remaining: Decimal = 0, min_secondary_remaining: Decimal = 0, snapshot_max_age_seconds: int = 120, retry_limit: int = 1, plan_order: tuple[str, ...] = (), prefer_expiring: bool = False)`; maximum age 30–3600 seconds, retry_limit 0 or 1. Empty plan order means no inferred preference.
- `PoolScope(customer_id: UUID, key_id: UUID, model_id: str, session_digest: bytes | None)`; opaque session identifier is HMACed with a domain-separated existing pepper, never stored/logged raw.
- `AccountCandidate(route: RouteSnapshot, quota: CodexQuotaSnapshot | None, health: str, cooldown_until: datetime | None, subscription_expires_at: datetime | None)`.

OAuth account list email is masked. Existing private `/credentials` semantics remain compatible. Store encrypted proxy entries using existing `ProfileStore`; the spec's “fingerprint” means secret-safe identity/logging, not discarding the encrypted key required for HTTP calls. Never put a plaintext key into new tables.

### Reset safety and read-only GETs

Private GET returns saved quota/credit snapshots and performs no upstream request, token refresh or Kiot allocation. `POST quota/refresh` explicitly fetches usage and reset credits under admin CSRF; results track partial failures separately. A missing field is unknown, not zero/100%. Keep the last successful snapshot on a refresh error and mark it stale.

Admin first confirms a reset with `{request_id, credits_version, acknowledge:true}`. The service rechecks availability (snapshot <=120s; stale requires refresh), locks the credential/reset state, and persists one operation. `request_id` is the durable operation/idempotency UUID; `redeem_request_id` is a distinct server-created UUID stored once. Reusing `request_id` with a different credential or payload returns 409. At most one prepared/dispatched/unknown/succeeded_refresh_failed operation per credential. Mark `dispatched` before HTTP POST; no network call while holding DB row locks.

No automatic consume retry, including after a 401: refresh tokens before sending; a definitive rejection is recorded and requires a new explicit confirmation after refresh. Timeout, connection loss, cancellation, 5xx with uncertain application or crash after dispatch => `unknown`. Reopening the UI or replaying the same request returns the saved operation, not another POST. Expired dispatched operations become unknown, not retryable. A successful HTTP consume followed by failed snapshot refresh => `succeeded_refresh_failed`. Fetching snapshots later can release the confirmed-success lock; it cannot prove whether an unknown consume succeeded just from a changed percentage/count. An unknown operation remains blocked pending explicit evidence-based admin resolution through `POST reset-requests/{id}/resolve` (`version`, `outcome`, bounded `reason`), with audit, and never issues another upstream POST.

Service deadlines: total 30 seconds, cleanup allowance 5 seconds, response JSON <=2 MiB, operation lease expiry checked from PostgreSQL clock. Per-credential fencing prevents an earlier delayed refresh overwriting a newer snapshot, and prevents reset while a disable/profile/account generation change invalidates its preflight. Build no in-process scheduler on startup.

### Proxy and routing

Precedence for token-refresh/quota/reset/inference: explicit credential profile (including a direct profile) > provider profile > direct. Introduce one resolver used by all four paths; no fallback to direct when a configured proxy fails. Resolve/validate profile versions before dispatch, keep an acquired lease until the HTTP request and cleanup finish. Do not call Kiot `/out` on every request: retain existing idle-only explicit release, honoring `/current`, `/new`, TTL and TTC. Rotation must not evade upstream cooldown.

Pool policy opt-in: no saved policy retains existing candidate ordering for backward compatibility. Policy saves require enabled Codex bindings for the same canonical public model. Snapshot metadata never determines public model authorization. All modes exclude disabled/reauth/uncertain/cooling accounts and fresh evidence of exhaustion. When quota reserve is enabled, missing, stale or failed-refresh quota is ineligible; absent secondary window is not invented (only require a window if its configured threshold >0). After a reported reset deadline passes, require new evidence before clearing exhaustion; elapsed time alone does not fabricate replenishment. An expired access token with usable refresh state is refreshable, not a permanently dead account.

`auto`: compare minimum known remaining fraction across reported windows, then configured plan order, then known subscription expiry only when prefer_expiring=true, then stable credential UUID. Unknown quota sorts after known unless reserve rejects it. `random` shuffles eligible members; `priority` sorts ascending; `weight` uses injected RNG with positive weights without replacement; backup members enter only after primary candidates are exhausted. `single` returns its selected member or unavailable. No implicit preference for a particular paid plan.

Native continuation scope already exists in `ContinuationStore`; preserve it. For optional affinity without a continuation, accept one bounded `X-Gateway-Session` header (1–200 printable ASCII, duplicate rejected), HMAC scope includes customer/key/canonical model. Do not claim arbitrary Codex clients send this gateway-specific header. Bind with first-writer-wins CAS before dispatch; concurrent first calls reuse winner. TTL 24h maximum. A continuation's account wins over header affinity; mismatch returns invalid_state. If bound account is unavailable, fail without silently moving its conversation. Unbound new requests may retry once on a definitive 429 before any accepted generation; cooldown only the credential for Codex, not all accounts sharing its provider. Respect provider-wide errors for non-Codex adapters unchanged.

### Capabilities and UI gates

This release supports tested HTTP text/tool Chat/Responses paths, and explicit rejection contracts for compact/images/WebSocket. No admin flag can enable unimplemented code. Runtime capabilities are the intersection of implemented adapter contract, published binding and account/model eligibility. Preserve unsupported options rather than silently dropping them.

One optional backend compatibility path is exactly `POST /backend-api/codex/responses`, mapped to the existing authenticated Responses handler, never a catch-all proxy. Reject other backend paths without upstream I/O. `/v1/responses/compact` stays `unsupported_feature` (HTTP 400 per existing mapping); image JSON routes likewise, non-JSON remains boundary 415. WebSocket upgrades close pre-accept (1008 / handshake rejection); do not promise a JSON HTTP body after upgrade.

Superdesign catalog v4 approval does not approve new account/reset/routing screens. Task 10 must extend the existing design incrementally and obtain visual approval before Task 11 product UI. This is a human approval gate, not an implementation TODO. Do not regenerate the dashboard or charge for alternative drafts the user did not request.

## Task 1: Pure cache normalization, rates and policy defaults

**Files:** create `metering/usage.py`; modify `metering/units.py`, `metering/records.py`, `identity/policy.py`, `routing/records.py` under `autobuild_json/gateway/`; modify normalizers in `providers/openai.py`, `responses_events.py`, `anthropic.py`, `gemini.py`; create `tests/gateway/unit/test_usage_normalization.py`; extend `test_units.py`, `test_key_policy.py`, `test_costs.py`.

**Consumes:** existing `Usage`, `Bounds`, `coefficient_micro`, `CostSchedule`.
**Produces:** `normalize_provider_usage(payload: dict, provider: str) -> Usage`, with provider in `openai_chat/openai_responses/codex/anthropic/gemini`; `weighted_usage_micro(usage: Usage, input_micro: int, output_micro: int, cache_read_micro: int | None = None, cache_write_micro: int | None = None) -> int`; `hold_micro(bounds: Bounds, input_micro: int, output_micro: int, cache_read_micro: int | None = None, cache_write_micro: int | None = None) -> int`.

- [ ] RED: add the following unit test, missing/negative/noninteger/bool/nonfinite usage rejection, cache sums >input rejection, Gemini reasoning-total case, zero cache rate and overflow cases:

```python
def test_cache_buckets_are_disjoint():
    from autobuild_json.gateway.metering.usage import normalize_provider_usage
    from autobuild_json.gateway.metering.units import weighted_usage_micro
    inclusive = normalize_provider_usage({"input_tokens": 1000, "output_tokens": 200,
        "input_tokens_details": {"cached_tokens": 600}}, "codex")
    exclusive = normalize_provider_usage({"input_tokens": 400, "output_tokens": 200,
        "cache_read_input_tokens": 600, "cache_creation_input_tokens": 0}, "anthropic")
    assert inclusive == exclusive
    assert weighted_usage_micro(inclusive, 1_000_000, 3_000_000, 100_000) == 1_060_000_000
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/unit/test_usage_normalization.py -q`; record actual missing-helper failure.
- [ ] GREEN: implement strict normalizer over current adapters' supported fields (do not infer tokens from output text); move the existing provider normalization behind thin compatibility wrappers. Implement arithmetic with explicit `None` handling:

```python
cr = input_micro if cache_read_micro is None else cache_read_micro
cw = input_micro if cache_write_micro is None else cache_write_micro
uncached = usage.input_tokens - usage.cached_read - usage.cached_write
total = uncached * input_micro + usage.cached_read * cr + usage.cached_write * cw + usage.output_tokens * output_micro
# Validate strict nonnegative integers and total < 10**38 before returning.
```

- [ ] Add nullable rate fields using the existing `Quota` constrained integer type; append dataclass fields after existing defaults. Test old constructors, policy JSON and upstream cost results unchanged. Cost rates do not set customer cache rates.
- [ ] Run `.venv/bin/python -m pytest tests/gateway/unit/test_usage_normalization.py tests/gateway/unit/test_units.py tests/gateway/unit/test_costs.py tests/gateway/unit/test_key_policy.py -q` and `.venv/bin/ruff check autobuild_json/gateway/metering autobuild_json/gateway/identity autobuild_json/gateway/routing/records.py autobuild_json/gateway/providers`; commit `feat: normalize cache-aware usage and rates`.

## Task 2: Atomic cache-rate snapshots and quota settlement

**Files:** create `storage/migrations/versions/0012_cache_rates.py`; modify `metering/ledger.py`, `providers/catalog.py`, `engine.py`, `catalog/records.py`, `catalog/context.py`, `catalog/publish.py`, `admin/serialization.py`; tests `integration/test_cache_ledger.py`, existing `test_ledger.py`, `test_ledger_races.py`, `test_catalog_schema.py`, `test_backups.py`.

**Consumes:** Task 1 helpers/rate fields, `Ledger.reserve/resize/settle` and `Usage`.
**Produces:** four effective rates frozen on each request; exact `computed_micro` and `charged_micro` in saved usage metadata (not wire codec usage); `RouteSnapshot` carries model defaults. Existing `Ledger` entry points stay compatible.

- [ ] RED: use existing `gateway_environment(pg_db, quota=100_000_000_000_000)` and real PostgreSQL. Add a model/key override with input=1e6, cache-read=1e5, cache-write=1.25e6, output=3e6; settle `Usage(1000,200,cached_read=600,cached_write=100)`. Assert spent=1_085_000_000, held=0 and limit-spent=99_998_915_000_000. Repeat settle concurrently: one settlement row, same totals. Mutate key/model rates during stream: original rates still charge. Include inherited defaults, explicit zero, cache-write >input hold maximum, UTC midnight and overflow rollback.
- [ ] Run `.venv/bin/python -m pytest tests/gateway/integration/test_cache_ledger.py -q`; record wrong-charge failures before implementation.
- [ ] GREEN: Alembic `0012_cache_rates` follows `0011_catalog_schedule_completion` (read actual revision value before writing). Add nullable cache columns, backfill from request input, then non-null/check >=0; no historical settlement rewrite. Use `hold_micro` in reserve/resize, `weighted_usage_micro` in settle. Preserve existing lock order and idempotency. Persist computed/charged metadata with existing actual usage, excess flag and binding quarantine.

```sql
UPDATE requests SET cache_read_micro=input_micro, cache_write_micro=input_micro
WHERE cache_read_micro IS NULL OR cache_write_micro IS NULL;
```

- [ ] Pass effective fields through catalog route/context/publish DTOs and `Engine` admission; ensure probe cost/quota isolation and old positional constructors remain valid. Exact management JSON emits new `_micro` fields as decimal strings.
- [ ] Run `.venv/bin/python -m pytest tests/gateway/integration/test_cache_ledger.py tests/gateway/integration/test_ledger.py tests/gateway/integration/test_ledger_races.py tests/gateway/integration/test_catalog_publish.py tests/gateway/integration/test_backups.py -q` and Ruff; commit `feat: settle cached tokens with frozen exact rates`.

## Task 3: Durable account state and operations schema

**Files:** create `accounts/quota_records.py`, `routing/pool_records.py`, `accounts/quota_store.py`, `storage/migrations/versions/0013_codex_accounts.py`; modify `storage/backup.py`; create `tests/gateway/integration/test_codex_service_schema.py`, `test_codex_quota_store.py`; extend schema/backup tests.

**Consumes:** shared DTO contracts and `Database.sessions`; encrypted credentials remain in existing vault/table.
**Produces:** `QuotaStore(db)` with `read(credential_id)`, `read_credits(credential_id)`, `begin_refresh(credential_id, deadline) -> RefreshClaim`, `save_refresh(claim, usage, credits, error_codes) -> bool`, `prepare_reset(credential_id, request_id, credits_version, actor) -> ResetResult`, `mark_dispatched(operation_id, generation) -> bool`, `finish_reset(operation_id, generation, state, code, status) -> ResetResult`, `recover_expired() -> int`, `resolve_reset(operation_id, version, outcome, reason, actor) -> ResetResult`. `RefreshClaim` carries credential id, generation, deadline, credential/profile/token versions only, no secrets.

- [ ] RED: real DB tests create two refresh claims, save newer then older => older rejected; duplicate reset request returns same operation; mismatched payload=>409; concurrent different reset IDs=>one accepted; backup/restore preserves unknown lock. Verify missing account FK and invalid state/check constraints fail.

```python
from sqlalchemy import text

async def test_new_account_tables_exist(pg_db):
    names = ("codex_account_state", "codex_reset_requests", "account_pool_policies", "account_session_bindings", "key_quota_adjustments")
    async with pg_db.sessions() as session:
        for name in names:
            assert await session.scalar(text("SELECT to_regclass(:name)"), {"name": name})
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/integration/test_codex_service_schema.py tests/gateway/integration/test_codex_quota_store.py -q` for RED.
- [ ] GREEN: migration follows Task 2; create tables with these exact responsibilities:

| Table | Keys and columns | Constraints |
|---|---|---|
| codex_account_state | credential_id PK/FK, version default1, usage_snapshot/credits_snapshot JSONB nullable, fetched_at/credits_fetched_at/last_attempt_at/error fields, generation default0, refresh_deadline nullable, last_success_at/last_failure_at, failure_count default0, subscription_expires_at nullable | version>=1, generation/failure_count>=0; never store raw body |
| codex_reset_requests | id UUID PK=request_id, credential_id FK, redeem_request_id UUID UNIQUE, payload_digest bytes, state, generation/version default1, actor, upstream_status/result_code nullable, requested_at/deadline/completed_at, resolution JSONB nullable | unique active credential partial index for prepared/dispatched/unknown/succeeded_refresh_failed; state allowlist; deadline>requested_at |
| account_pool_policies | model_id PK/FK, version default1, policy JSONB | version>=1 |
| account_session_bindings | scope_digest bytes PK, customer_id/key_id/model_id FKs, binding_id/credential_id FKs, expires_at, version default1 | digest length32, expiry index; no raw session value |
| key_quota_adjustments | id UUID PK, key_id FK, version_before/version_after, before_limit_micro/after_limit_micro nullable, amount_micro nonnegative, actor, reason, created_at | adjustment id replay must match payload; no deletion of ledger |

Add nullable provider_id/credential_id/binding_id and config_version snapshot columns to `attempts` with FKs; old rows stay null/unknown, never backfilled using current routing guesses. Add index `(credential_id,started_at)`. The nullable `attempts.credential_id` foreign key points to `credentials.id` and is inserted after `credentials` in backup order. Extend backup TABLES in FK order and test same-revision restore; old backups require their matching older schema per existing contract.
- [ ] Store methods lock credential -> account state -> reset operation, ordered UUIDs for multi-account batches; do not hold across HTTP. Separate from customer ledger transactions. Audit reserve/transition/resolution in same transaction. Expired prepared without dispatch can be rejected safely; expired dispatched becomes unknown. Add safe finite error codes to `errors.py` rather than persisting arbitrary messages.
- [ ] Run focused tests + `test_backups.py` + `test_catalog_schema.py`, Ruff and diff checks; commit `feat: persist fenced Codex account operations`.

## Task 4: Read quota and credits through consistent proxy selection

**Files:** create `accounts/proxy_selection.py`, `accounts/quota_client.py`, `accounts/quota.py`; modify `accounts/imports.py`, `admin/services.py`, `transport/http.py`, `errors.py`; tests `unit/test_codex_quota_client.py`, `integration/test_codex_quota.py`, existing refresh/proxy/transport tests.

**Consumes:** Task 3 records/store; CredentialService.fresh_tokens, ProfileStore, Transport and ProxyManager.
**Produces:** `AccountProxyResolver(db, profiles).resolve(credential_id) -> ProxySelection`; `QuotaClient(transport).usage(route, lease, tokens, account_id, deadline) -> dict`; `credits` with same signature; `CodexQuotaService(db, credentials, proxies, profiles, transport, store).refresh(credential_id, deadline) -> CodexQuotaSnapshot`; `get`/`get_credits` are storage-only.

- [ ] RED parser tests: missing window=>None; used_percent=25=>75 remaining; primary window_seconds=604800 stays weekly; negative/NaN/>100 percentages rejected; reset_at wins over relative, negative relative rejected; unknown credit status not spendable; absent available_count derived only from explicit unexpired available records. Verify malformed payload preserves prior snapshot plus safe error.
- [ ] Add real-service fake-transport tests for credential fixed proxy overriding provider Kiot; inherited provider proxy; explicit direct profile overriding provider proxy; failure never falls back direct; all four modes; 401 refresh preflight; old token/profile snapshot fenced. GET projections create zero outbound requests.
- [ ] Run `.venv/bin/python -m pytest tests/gateway/unit/test_codex_quota_client.py tests/gateway/integration/test_codex_quota.py -q` for RED.
- [ ] GREEN: separate guarded route root is exactly `https://chatgpt.com/backend-api/wham`, not a configured arbitrary root. Add request kind `codex_account`, permitting ONLY `(GET,usage)`, `(GET,rate-limit-reset-credits)`, `(POST,rate-limit-reset-credits/consume)`, no query, auth bearer plus chatgpt-account-id; no cookies or redirect following. Preserve discovery/inference validation unchanged.

```python
ACCOUNT_CALLS = {("GET", "usage"), ("GET", "rate-limit-reset-credits"),
                 ("POST", "rate-limit-reset-credits/consume")}
# Reject a different root, query, suffix, body shape or untrusted forwarded header.
```

- [ ] Resolve one effective selection policy per operation, refresh tokens before acquiring the operation lease (avoid nested exclusive Kiot lease deadlock), then validate policy/version unchanged before dispatch. Keep lease through read/close. Total deadline bounds DB, proxy, refresh and HTTP; join owned helpers on cancellation, no detached HTTP task. Reuse refresh fencing, never set uncertain credentials active from quota metadata alone.
- [ ] Parse allowlisted fields only; persist partial usage/credits successes with separate timestamps/error codes. Fix refresh provider inheritance to match inference, but do not change configured endpoints or live profiles.
- [ ] Run focused tests, `test_refresh_races.py`, `test_gateway_transport.py`, `test_proxy_manager.py`, `test_kiot.py`; commit `feat: fetch Codex quota through account proxies`.

## Task 5: Explicit upstream reset-credit consumption and recovery

**Files:** create `accounts/reset_credits.py`; modify `accounts/quota.py`, `accounts/quota_client.py`, `maintenance.py`, `admin/services.py`; tests `integration/test_codex_reset_credits.py`, `unit/test_codex_reset_recovery.py`.

**Consumes:** durable QuotaStore and Task 4 proxy/client helpers.
**Produces:** `ResetCreditService(...).consume(credential_id, request_id, credits_version, acknowledge, actor, deadline) -> ResetResult`; `resolve(operation_id, version, outcome, reason, actor)`; maintenance explicitly calls `recover_expired` with no consume retry.

- [ ] RED real DB tests: simultaneously confirm same UUID=>one POST; distinct UUID while pending=>409 and one POST; same UUID other credential=>409; zero/unknown credits=>no POST; stale version=>409; unauthenticated admin handled Task9. HTTP200 then refresh failure=>succeeded_refresh_failed; dropped POST response=>unknown; replay/request reconnect/maintenance=>no second POST; malformed error body sentinel never appears in DTO/audit/caplog. Delayed completion with old generation cannot resolve new state.
- [ ] Run `.venv/bin/python -m pytest tests/gateway/integration/test_codex_reset_credits.py tests/gateway/unit/test_codex_reset_recovery.py -q` for RED.
- [ ] GREEN: implement the state machine from Shared contracts. Dispatch CAS gates transport:

```python
operation = await store.prepare_reset(credential_id, request_id, credits_version, actor)
# Existing operations return their stored result; only a new live operation proceeds.
# Obtain fresh tokens/proxy outside the transaction and revalidate the claim.
if await store.mark_dispatched(operation.operation_id, operation.generation):
    body = {"redeem_request_id": str(operation.redeem_request_id)}
    # Send exactly once with the guarded client, then persist success/rejection/unknown.
```

Keep secret/internal claim fields in a private store record separate from public `ResetResult`; define `ResetClaim(result, generation, redeem_request_id, is_new)` for `prepare_reset` return internally and expose `.result` to callers. Definitive 400/401/403/404/409/422/429=>rejected (429 stores Retry-After); timeout/ambiguous5xx/disconnect=>unknown. HTTP2xx proves accepted only, never fabricated quota100%. Every transition audits once.
- [ ] Resolution requires explicit outcome `confirmed_applied`/`confirmed_not_applied`, version, actor and reason<=500 chars; reject obvious secret text/control chars, immutable audit retained. Confirmed outcomes release lock but do not manufacture a fresh quota snapshot. Resolution never adjusts customer quota.
- [ ] Run new tests plus `test_recovery.py` and existing OAuth/refresh suites; commit `feat: consume Codex reset credits exactly once per confirmation`.

## Task 6: Pool selection and tenant-scoped affinity

**Files:** create `routing/account_pool.py`, `routing/pool_store.py`; modify `providers/catalog.py`, `routing/records.py`, `routing/continuations.py` only for integration hooks; tests `unit/test_account_pool.py`, `integration/test_account_pool.py`.

**Consumes:** `AccountPoolPolicy/PoolScope/AccountCandidate` from Task3, existing authorized candidates/ContinuationStore.
**Produces:** pure `order_candidates(policy, candidates, now, rng) -> tuple[RouteSnapshot, ...]`; `PoolStore.get(model_id) -> (version, policy) | None`, `put(model_id, expected_version, policy, actor) -> int`; `bind(scope, route, expires_at) -> RouteSnapshot`, `resolve(scope) -> RouteSnapshot | None`. Store generation/version/CAS return shapes must be documented in the task report.

- [ ] RED unit test every named ordering and exclusion, injected deterministic RNG, duplicate/invalid weights, backups only after primary; primary absent and threshold>0 fail-closed; reset deadline elapsed does not assume fresh quota. Integration tests same session races to one account, same session string across customer/key/model cannot collide, expired handle, disabled pinned account no failover, version conflict.

```python
def test_policy_cannot_raise_retry_budget():
    import pytest
    from pydantic import ValidationError
    from autobuild_json.gateway.routing.pool_records import AccountPoolPolicy
    with pytest.raises(ValidationError):
        AccountPoolPolicy(mode="random", members=(), retry_limit=2)
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/unit/test_account_pool.py tests/gateway/integration/test_account_pool.py -q` for RED.
- [ ] GREEN: filter candidates already authorized by catalog; never authorize from quota or pool metadata. CAS binding with `INSERT ... ON CONFLICT DO NOTHING` then read winner; lock order model/pool -> affinity row; recheck live credential before network. No customer locks held while acquiring pool locks. Inject RNG only for tests; never a module-global seed affecting production.
- [ ] Default no-policy keeps prior ordering. Preserve native continuation credential/binding checks; malformed or mismatched external scope gives invalid_state. Quota reserve opt-in and backup choices appear in safe diagnostic codes, not raw upstream messages.
- [ ] Run new tests + `test_catalog.py`, `test_continuations.py`, `test_routing.py`; commit `feat: route Codex pools with scoped affinity`.

## Task 7: Integrate credential-aware dispatch, attribution and retry

**Files:** modify `engine.py`, `providers/codex.py`, `providers/catalog.py`, `metering/ledger.py`, `http/app.py`, `http/auth.py`; create `routing/session.py`; tests `integration/test_codex_pool_dispatch.py`, extend `test_codex_gateway.py`, `test_cooldown.py`, `test_stream_lifecycle.py`, `test_review_lifecycle.py`.

**Consumes:** Task2 accounting, Task4 shared proxy resolver, Task6 PoolStore/order_candidates.
**Produces:** `RequestMeta.session_digest: bytes | None = None`, `Ledger.mark_dispatched(request_id, attempt_id, *, route=None)` storing attribution at dispatch; `Catalog` safe credential outcome updates. Old callers keep null attribution.

- [ ] RED: two active Codex accounts under SAME provider, first returns definitive429 then second success => two attempts, one customer settlement, first credential cooldown only. No failover for pinned continuation/session; no retry timeout or partial SSE. Missing usage=>hold remains. Auth/profile mutation before dispatch=>no HTTP. Assert selected account header and actual proxy on retry, attempt credential IDs and terminal usage; cancel cleanup releases admission/proxy leases.
- [ ] Run `.venv/bin/python -m pytest tests/gateway/integration/test_codex_pool_dispatch.py -q` for RED.
- [ ] GREEN: integrate pool order after model/key authorization and before reservation. Snapshot effective rates once; resize conservative bounds if safe fallback differs. Permit distinct credentials under one Codex provider only for unbound safe pre-generation retry; keep existing non-Codex retry behavior. Do not perform live quota GET/consume in public request handling.

```python
# Codex rejection receipt; do not cool every sibling credential.
await catalog.cooldown(selected.provider_id, selected.credential_id,
                       retry_seconds, started_at=received_at)
# For a non-Codex provider retain its existing provider-wide policy.
```

- [ ] HTTP optional `X-Gateway-Session` header parsing rejects duplicates/control/oversize values, creates HMAC scoped digest, never forwards it upstream. Default no header uses only current native continuation affinity. Existing public JSON bodies remain unchanged.
- [ ] Add successful/failed attempt metadata updates, safe health codes, no token health resurrection on transport success. Preserve first response event, collector ID, tools, cancellation and single-settle flow. No `immediateSseResponse` feature added.
- [ ] Run new tests, existing Codex/Responses/gateway/cooldown/stream/refresh and SDK suites; commit `feat: dispatch and account for Codex pools safely`.

## Task 8: Honest compatibility and model-policy surface

**Files:** create `providers/codex_capabilities.py`, `http/codex_routes.py`; modify `http/app.py`, `http/boundary.py`, `identity/policy.py`, `providers/catalog.py`; tests `integration/test_codex_http.py`, `unit/test_codex_capabilities.py`, `integration/test_model_visibility.py`.

**Consumes:** supported existing OpenAI codecs and Task7 prepared-call path; do not create a second ledger path.
**Produces:** `codex_capabilities() -> dict` fixed capability version `codex-http-v1`, true `chat/responses/text/tools`, false `compact/images/websocket`; GET service capability projection added Task9. `KeyPolicy.excluded_model_ids=frozenset()` and `model_prefix=''` default-compatible; prefix max64 model-safe ASCII chars, no wildcard patterns.

- [ ] RED exact routes: models/chat/responses JSON/SSE pass using fake transport. Compact/images=>authenticated unsupported400 with zero dispatch; backend only exact POST responses works, traversal/arbitrarysuffix reject; WS rejects pre-accept without upstream socket. Unknown option remains unsupported rather than silently ignored.
- [ ] RED policy tests `model_prefix='team/'`, allow `public`, exclude `blocked`: `/v1/models` returns team/public; request team/public authorizes canonical public; raw public rejected while prefix configured; exclude wins over all_models. Aliases canonicalize before policy checks; all input protocols use same name resolution. Collision/ambiguous alias-prefix strings fail validation rather than expose extra model.
- [ ] Run `.venv/bin/python -m pytest tests/gateway/integration/test_codex_http.py tests/gateway/integration/test_model_visibility.py tests/gateway/unit/test_codex_capabilities.py -q` for RED.
- [ ] GREEN: shared handlers for `/v1/responses` and exact `/backend-api/codex/responses`. Authenticate before typed unsupported HTTP responses. No catch-all upstream forwarding. WS route closes before accept with code1008; no unimplemented flag can enable it. Native Codex/CodexCLI options outside these tested HTTP codecs remain explicitly unsupported.

```python
CAPABILITIES = {"version": "codex-http-v1", "chat": True, "responses": True,
                "text": True, "tools": True, "compact": False,
                "images": False, "websocket": False}
```

- [ ] Model visibility and dispatch share policy helper for prefixed names, exclusions, enabled state and canonical identity. Preserve raw token count response semantics and upstream model rewriting. No claims of image slot scheduling or full desktop profile compatibility in this release.
- [ ] Run new tests and all protocol/SDK suites; commit `feat: define tested Codex HTTP compatibility and key visibility`.

## Task 9: Private admin operations, balances and account reports

**Files:** create `admin/codex_accounts.py`, `admin/codex_schemas.py`, `metering/adjustments.py`; modify `admin/services.py`, `admin/routes.py`, `admin/usage.py`, `admin/serialization.py`, `security.py`, `identity/service.py`; tests `integration/test_codex_admin.py`, `test_codex_usage_reports.py`, `test_key_quota_adjustment.py`.

**Consumes:** all account/pool/reset services, immutable attempts, ledger amounts and private ExactJSONResponse.
**Produces:** APIs below; all mutation DTOs inherit StrictInput/VersionInput, deny extra fields, fixed actor from existing authorized admin identity.

| Method/path under `/api/service` | Contract |
|---|---|
| GET oauth-accounts | paged `{items,next_after}` masked email/status/plan/proxy reference only; limit1–200, after UUID; no HTTP to upstream |
| PATCH oauth-accounts/{id} | `{version,enabled,proxy_profile_id}`; optimistic update, validate OAuth identity/profile, audit, reject during unresolved reset; no token response |
| GET oauth-accounts/{id}/quota | latest snapshot, stale/last_error and timestamp; no upstream I/O |
| POST oauth-accounts/{id}/quota/refresh | `{}`; deadline30s; fetch usage+credits; separate partial status |
| GET oauth-accounts/{id}/reset-credits | saved credits and active reset operation; no upstream I/O |
| POST oauth-accounts/{id}/reset-credits/consume | `{request_id,credits_version,acknowledge:true}`; returns ResetResult, 202 for dispatched/unknown, 200 for terminal saved result |
| POST reset-requests/{id}/resolve | `{version,outcome,reason}`; evidence-based audited resolution only |
| GET/PUT account-pools/{model_id:path} | version+policy; PUT `{version,policy}`, zero version means create-only; authorize canonical model |
| GET usage/accounts and GET usage/requests | credential_id/model_id/from/to/limit (1–200)/after UUID; UTC interval <=31 days, default last24h; parameterized SQL |
| POST keys/{id}/quota-adjust | `{request_id,version,amount_micro,reason}`; amount>=0, grant total limit by delta, replay identical returns receipt, payload mismatch409 |
| GET capabilities | static tested matrix plus existing other adapter support; admin only |

`quota-adjust` increases the key's total limit, does not reduce spent or clear held. To restore 100m AVAILABLE, admin grants only the displayed consumed/held difference explicitly; this endpoint does not silently reset to 100m. Unlimited total rejects adjustment. Day/month windows unchanged. Save before/after/actor/reason/idempotency receipt; same customer->key lock order as Ledger and exact overflow checks.

Usage reports separate attempts (including failed/internal operational costs) from customer settled quota. Attribute customer charge ONLY to the completed serving attempt; old missing-attribution stays unknown. Sum canonical input/cache/output counters without joining one settlement onto every failed attempt. Counts use database SUM with decimal-string serialization in private reports for values beyond JS safe integer; preserve public usage schemas.

- [ ] RED: actual private session/CSRF requests plus public404, invalid origin, duplicate/query secret fields, masked email, unknown/stale snapshot, fake secret sentinel in upstream error never appears in response/audit. Same request adjustment twice increments once; concurrent adjust/settle keeps exact total; old version409. Retry account totals charge only successful account.

```python
async def test_adjustment_does_not_delete_accounting(pg_db):
    from sqlalchemy import text
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT to_regclass('key_quota_adjustments')"))
# Extend this with existing admin_env, key policy and real ledger fixtures:
# initial total=100m, spent=1085, held=0; grant1085 -> total=100001085,
# spent still1085, original settlement row still present, available=100m.
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/integration/test_codex_admin.py tests/gateway/integration/test_codex_usage_reports.py tests/gateway/integration/test_key_quota_adjustment.py -q` for RED.
- [ ] GREEN: mount on existing private router only; exact query allowlist in BoundaryMiddleware for these GET endpoints, unknown/duplicate keys fail, no blanket `/api/service/*` exception. Legacy OAuth token-refresh action remains separate and unchanged. All mutating operations invalidate stale UI versions.
- [ ] Run new suites, `test_admin.py`, `test_catalog_admin.py`, `test_gateway_boundary.py` and Ruff; commit `feat: administer Codex accounts and exact usage`.

## Task 10: Incremental account UI design and visual approval

**Files:** update `docs/gateway-implementation-decisions.md`; validated `.superdesign/resume.json` and selected design artifacts only. No product UI in this task.

**Consumes:** approved current bronze admin shell, catalog draft v4, Task9 endpoint/wire contract.
**Produces:** user-approved account/quota/reset/pool and cache-rate UI states with draft/version/date, screen-to-endpoint mapping.

- [ ] Use Superdesign skill resume validation and existing project/draft; do not repeat init or cold-create a dashboard. Prepare synthetic screenshot/data and a minimal incremental draft, present the canvas URL, and wait for an explicit approval response before continuing. Any required source upload follows skill disclosure and permission boundaries.
- [ ] Show account list/detail, quota windows with unknown/stale state, credit availability/expiry, reset confirmation with pending/unknown/succeeded_refresh_failed states, disabled actions while uncertain, admin evidence-resolution form, proxy selector, pool editor, key/model cache rates, exact balance breakdown and grant quota confirmation.
- [ ] Include tooltip/default inheritance versus explicit0, no auto-reset, no credential fields in DOM or localStorage, quota percentage is not token count. API key100m example must show raw versus weighted and held/spent/available separately.
- [ ] Ask for visual approval of these added screens; STOP here until received. Record decision and resume next task without treating catalog v4 approval as approval of these new screens. No generated image assets required.
- [ ] Run `git diff --check` and commit only approved design mapping docs `docs: approve Codex account admin design`.

## Task 11: Admin UI integration with state and browser tests

**Files:** create `admin/static/codex-state.js`, `codex-view.js`, `codex-actions.js`; modify `admin/static/index.html`, `app.js`, `forms.js`, `keys.js`, `style.css`; extend `tests/gateway_ui_server.py`; create `tests/ui/codex-state.test.mjs`, `tests/ui/codex-browser-smoke.mjs`; modify `package.json`.

**Consumes:** Task10 visual approval and Task9 real wire contracts.
**Produces:** incremental “Codex accounts” tab, settings/forms/reports; Node pure state tests plus Chrome test using the real synthetic FastAPI fixture, NOT a disconnected static HTML mock.

- [ ] RED state tests for server-authoritative reset state, same request UUID retained through ambiguous response, selecting another account/logging out drops stale async result, unknown quota displays “Không rõ”, amount conversion uses BigInt/existing decimal helpers and never Number for quotas.

```javascript
import assert from 'node:assert/strict';
import test from 'node:test';
import {canConsumeReset} from '../../autobuild_json/gateway/admin/static/codex-state.js';
test('unknown reset outcome is not a new reset opportunity', () => {
  assert.equal(canConsumeReset({available_count: 2, fresh: true, operation_state: 'unknown'}), false);
});
```

- [ ] Run `node --test tests/ui/codex-state.test.mjs` for RED. Then extend synthetic fixture/Chrome smoke to click refresh/reset-confirm, simulate success-then-refresh-failure and recover via refresh only, verify no second POST. Assert model/key rates inheritance/zero roundtrip, 100m exact example, audit, pool save version conflict, mobile390px, keyboard focus/ARIA, DOM redaction and no external browser network.
- [ ] GREEN: split state, rendering and API actions; preserve auth/logout session epochs. Display upstream reset versus internal quota grant as separate labeled actions. Never auto-retry POST on click/network timeout. GET is list-only, periodic polling if used is storage-only and stops on logout/navigation. Reset refresh uncertainty cannot be cleared by a toast/dismiss.
- [ ] Reuse existing safe `node()/textContent` render helpers and session API; no raw upstream HTML, token previews or copy source UI. Include fixed/pool/Kiot/direct settings through existing ProfileStore DTOs.
- [ ] Run `npm run test:ui`, `npm run test:browser`, `npm run test:gateway-browser`, `node tests/ui/codex-browser-smoke.mjs`; save synthetic screenshots for desktop/mobile and inspect with view_image. Run `git diff --check`; commit `feat: add safe Codex account control UI`.

## Task 12: Documentation, full verification and independent branch review

**Files:** update `README.md`, `CHANGELOG.md`, `docs/gateway-implementation-decisions.md`; create `tests/gateway/integration/test_codex_service_e2e.py`; extend `tests/ui/acceptance.md`.

**Consumes:** reviewed Tasks1–11 and unchanged legacy OAuth/catalog/public protocol contracts.
**Produces:** checked compatibility matrix, operating guide, evidence report and final whole-branch reviewer verdict. This is not deployment approval.

- [ ] RED e2e tests combine real identity/ledger/PG/proxy manager with fake network: 100m key -> Codex account pool -> one 429 -> SSE success with cached usage -> per-account usage -> admin read/refresh quota -> explicit reset UUID -> read new quota; replay consume does not repeat. Separate crash/unknown path never silently resets. Assert admin operations do not change customer ledger.
- [ ] GREEN compose the existing synthetic fixtures only; fix missing integration through original task implementers. Document request examples, direct/fixed/pool/Kiot precedence, separate quota tiers, reset eligibility/uncertain recovery, key credit adjustments, tested protocol subset, private-route protection, and absent compact/images/WS/profile takeover.
- [ ] Run all following on the final snapshot, sequentially for shared browser/database resources; do not substitute old test counts for current output:

```bash
.venv/bin/python -m pytest -q
.deps/venv310/bin/python -m pytest -q
.venv/bin/ruff check .
.deps/venv310/bin/ruff check .
npm run test:ui
npm run test:browser
npm run test:gateway-browser
node tests/ui/codex-browser-smoke.mjs
.venv/bin/python -m compileall -q autobuild_json
.venv/bin/python -m build
git diff --check
```

Use existing dedicated PostgreSQL harness with outbound network denied; migrations happen only in generated test schemas. If Python3.10/Chrome/binaries missing, record blocked checks, do not label full verification passed or mutate production env to supply them. Build uses already-installed deps if offline; report exact failures.
- [ ] Review diff/fixtures/log captures for secret sentinels and wrong schema defaults. Record test commands/results, known baseline warnings, supported versus rejected capabilities and pending provider-catalog UI separately. Update CHANGELOG under Unreleased, not a fictitious release.
- [ ] Commit docs/test additions; dispatch one independent whole-branch review with spec, plan, base-to-HEAD package, task ledger and deferred findings. Fix/re-review using SDD rules. Ask separately for integration choice; no merge/push/deploy from this plan alone.

## Self-review coverage and execution handoff

| Approved spec section | Implementation/test gate |
|---|---|
| Existing HTTP API and error/stream contract | Tasks7/8, real HTTP/SDK regressions |
| Account identity/refresh/security/proxies | Tasks3/4/7/9; token lease and precedence tests |
| OpenAI quota and limited reset credits | Tasks3/4/5/9/11; durable unknown and explicit confirm |
| Auto/random/single/priority/weight/session | Tasks6/7/9/11; cross-tenant and same-provider tests |
| 100m quota, cache/write/reasoning, defaults | Tasks1/2/9/11; old/new rate and parallel settlement tests |
| Per-account usage, key grants, immutable audit | Tasks3/7/9; failed attempts not double-counted |
| Migration/backups/no automatic scheduler | Tasks2/3/5/12; generated-schema only |
| Existing UI and new visual approval | Task10 before11; catalog work tracked separately |
| Conditional compact/backend, future image/WS | Task8 explicit supported/rejected contract; no fake feature flag |
| Full branch/doc/security verification | Task12, independent review and explicit integration choice |

The user reviews THIS written plan next. After approval, preserve subagent-driven
execution: use one fresh implementer per task, independent task-scoped review,
and a single whole-branch review at the end. Record each task's base commit before
dispatch; provide task brief plus these shared contracts and global constraints.
Do not pass the whole plan to every worker. No implementer writes in parallel.
