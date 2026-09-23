# AutoBuildJsonCockplit-Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local FastAPI/web UI tool that imports authorized accounts, performs HTTP Codex OAuth sequentially or concurrently through optional proxies, and exports compatible success JSON plus sanitized failure reports.

**Architecture:** Reuse the pinned Check-Account-ChatGPT auth methods behind a guarded HTTP adapter. Keep parser, OAuth/PKCE, auth integration, durable results, scheduling, and UI/API boundaries separate. Each attempt owns its cookies, verifier, state, proxy, deadline, and cancellation checks; no 9router server or callback listener is required.

**Tech Stack:** Python 3.10+, FastAPI, Uvicorn, Pydantic Settings, curl_cffi, pyotp, PyJWT[crypto], pytest, HTTPX, Ruff; local HTML/CSS/JavaScript without a frontend runtime build requirement.

**Spec:** `docs/superpowers/specs/2026-09-23-autobuild-json-cockplit-design.md` — approved by the user's “triển khai đi” response. This implementation plan still requires review and execution-method selection before product code.

## Global Constraints

- Project name: `AutoBuildJsonCockplit-Tools`; transport: HTTP; no Playwright in the product.
- Reference auth repository: `https://github.com/tmq9999/Check-Account-ChatGPT.git`, commit `791beb350370c191bbe8ddbe0b4a3fa0072620d9`; do not modify the user's reference projects.
- Input: exactly `Email|Password|2FA Secret`; maximum 5 MiB and 10.000 lines; preserve password characters, never modify the input file.
- One Uvicorn process, one active batch; `sequential` or `parallel`; 1–16 workers, default 1.
- One in-flight attempt per email and per configured proxy; sticky proxy throughout an attempt; no automatic direct fallback.
- Request timeout at most 30 seconds; default account deadline 180 seconds; one restart at most for `invalid_state`; never blindly retry authorization-code redemption.
- PKCE S256; 10-minute state lifetime; single-use callback; at most 100 pending manual sessions.
- Redirect URI `http://localhost:1455/auth/callback`; intercept it before making a network request; no callback listener.
- Client ID `app_EMoamEEZ73f0CkXaXp7hrann`; default app version `26.820.60940`; preserve all authorize/wrapper parameters specified in the spec.
- Success JSON contains only `id`, `email`, `account.id`, `tokens.id_token`, `tokens.access_token`, `tokens.refresh_token`.
- Exports: `success.json`, `errors.json`, `phone_verify.json`, `filtered.json`, `cancelled.json`; phone label is exactly `Phone number verify`.
- Default bind `127.0.0.1`; local admin session, Host/Origin/CSRF checks; no wildcard CORS; credential responses are `no-store`.
- No password/secret/raw HTTP response in logs, API errors, persistence, UI polling, or design-service context; token export is the explicit exception for successful credentials.
- `data/runs/` uses private permissions and atomic snapshots; previously persisted success survives cancellation/restart.
- No live account login during automated tests; synthetic HTTP/JWT fixtures only. Real login requires a separate user-selected account and explicit run request.
- No additional CAPTCHA/Turnstile solver, phone automation, subscription checker, account creation, or bypass of account restrictions.

## Review Focus

1. Whitespace, BOM, duplicate emails, and delimiter characters must not silently change passwords or drop accounts — Task 1.
2. Concurrent callback submissions and request timeouts must not consume one code twice or export another account's tokens — Task 2.
3. An upstream profile `phone_number` field must not be mistaken for a phone-verification challenge; legacy retries/debug must not leak secrets — Task 3.
4. Stop, disk-full, and restart between exchange and persistence must preserve confirmed results and accurately label unfinished work — Tasks 4–5.
5. Malformed JSON, validation errors, malicious Origin/Host, and direct export requests must not leak account rows, proxy passwords, or OAuth tokens — Task 6.

## File and interface map

All paths below are relative to the new project, not to `Auto-Oauth-Codex-9Router`.

| Files | Responsibility |
|---|---|
| `pyproject.toml`, `.gitignore`, `.env.example` | Package, dependencies, safe defaults, exclusions |
| `autobuild_json/__init__.py`, `models.py`, `errors.py`, `settings.py` | Explicit internal/public types and configuration |
| `autobuild_json/input_parser.py`, `proxies.py` | Strict account import and proxy normalization |
| `autobuild_json/oauth.py`, `tokens.py`, `transport.py` | PKCE lifecycle, callback validation, exchange/JWKS and guarded HTTP |
| `autobuild_json/checklive_adapter.py`, `challenges.py`, `navigation.py` | Pinned auth integration and state-machine navigation |
| `autobuild_json/results.py`, `runner.py` | Atomic persistence and bounded worker scheduling |
| `autobuild_json/security.py`, `api_models.py`, `api.py`, `__main__.py` | Local authentication, safe API DTOs, routes and launcher |
| `autobuild_json/static/index.html`, `app.js`, `style.css` | Vietnamese UI, file import, progress and downloads |
| `package.json` | Dev-only ES-module mode and Node UI test script; no runtime dependency installation |
| `scripts/setup_checklive.py`, `README.md`, `THIRD_PARTY.md` | Controlled dependency setup, operation and provenance |
| `tests/test_*.py`, `tests/fakes.py`, `tests/conftest.py` | Unit/integration tests, deterministic fakes, outbound-network denial |
| `tests/ui/*.test.mjs`, `tests/ui/acceptance.md` | Node built-in tests of UI logic and browser acceptance checklist |

### Shared contracts

Define the following in Task 1; later tasks consume these exact names:

- `AccountInput(account_job_id: str, line_number: int, email: str, password: str, secret: str)`: frozen dataclass, `password` and `secret` excluded from repr.
- `RejectedLine(line_number: int, code: str, reason: str)` and `ParseReport(accounts: list[AccountInput], rejected: list[RejectedLine], duplicate_lines: list[int])`.
- `ProxyConfig(server: str, username: str | None, password: str | None)`: credentials excluded from repr; `.url` is for transport only; `.key` is a nonsecret digest of the full normalized proxy identity.
- `FlowError(code: str, stage: str)`: safe exception whose message is resolved from a fixed catalogue; no raw exception input.
- `SuccessRecord(id: str, email: str, account: AccountIdentity, tokens: TokenSet)`, `AccountIdentity(id: str)`, `TokenSet(id_token: str, access_token: str, refresh_token: str)`: Pydantic models; token values excluded from repr.
- `RunResult(status: str, record: SuccessRecord | None = None, error: FlowError | None = None, attempt: int = 1)`: enforce that success has a record and non-success does not; preserve the final attempt number after a permitted restart.
- `AttemptContext(deadline: float, cancel: threading.Event, on_stage: Callable[[str, int], None])`: `.check()` raises safe `CANCELLED` or `TIMEOUT` errors, `.remaining_timeout()` returns `min(30, remaining_seconds)`; progress calls supply stage and attempt number.
- `Processor = Callable[[AccountInput, ProxyConfig | None, AttemptContext], RunResult]`.
- `Settings`: app host/port, private data path, `CHECKLIVE_PATH`, app version, stable installation UUID, and admin bootstrap-token source. OAuth hosts/client/redirect are not editable through API.

Public job snapshots are plain dictionaries built explicitly by the result store; never serialize `AccountInput`, `ProxyConfig`, exceptions, or internal OAuth objects to API responses automatically.

The public snapshot contract is `{id, status, counts, rows, batch_error}`. Batch status is one of `queued`, `running`, `stopping`, `completed`, `cancelled`, `error`; counts contain all six account statuses from the spec. Each row exposes only `account_job_id`, `line_number`, `email`, `status`, `stage`, `attempt`, `code`, `reason`, `timestamp`. `batch_error` is null or a safe `{code, reason}` object. `completed` means the batch finished processing, not that every account succeeded. Batch Stop sets `cancelled` once active attempts drain; disk failure sets `error`.

## Task 1: Runnable package, input contracts and proxy validation

**Files:** Create the package/configuration/models/parser/proxy files in the map; create `tests/test_input_parser.py`, `tests/test_proxies.py`, `tests/test_models.py`, `tests/conftest.py`.

**Interfaces:** Produce all shared contracts plus `parse_accounts(text: str) -> ParseReport` and `parse_proxies(text: str) -> list[ProxyConfig]`. Parsing never opens network connections or changes user files.

- [ ] **1. Execution preflight.** After plan approval, read the selected execution skill and `using-git-worktrees`. Confirm the new project contains only planning documents and that the reference's untracked `json.txt` is untouched. Initialize Git only inside this new project if appropriate under the isolation skill; never initialize the workspace parent. Read `test-driven-development` before implementation. The observed interpreter is Python 3.14.4; confirm compatible dependency distributions in a project venv, never change system Python. Report a real dependency compatibility blocker instead of pretending another interpreter exists.

```bash
git status --short
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip --version
```

- [ ] **2. Write parser/model/proxy failing tests.** Use this test plus parameterized cases for missing/empty/extra fields, malformed UTF-8 decoded at the API boundary, 5 MiB/10.000 line limits, non-Base32 secrets, duplicate emails, and malformed proxy ports/credentials.

```python
from autobuild_json.input_parser import parse_accounts
from autobuild_json.proxies import parse_proxies

def test_preserves_password_and_filters_without_leaking():
    report = parse_accounts(
        "\ufeffu@example.com| pass:word |JBSWY3DPEHPK3PXP\r\n"
        "bad@example.com|secret-password|\n"
        "u@example.com|another|JBSWY3DPEHPK3PXP\n"
    )
    assert report.accounts[0].password == " pass:word "
    assert [r.line_number for r in report.rejected] == [2]
    assert report.duplicate_lines == [3]
    assert "secret-password" not in repr(report.rejected)
    assert " pass:word " not in repr(report.accounts[0])

def test_proxy_credentials_are_encoded_and_private():
    proxy = parse_proxies("http|127.0.0.1|8080|a@b|p%ss")[0]
    assert proxy.url == "http://a%40b:p%25ss@127.0.0.1:8080"
    assert "p%ss" not in repr(proxy)
```

- [ ] **3. Verify red.** Create minimal test/dependency configuration, install only in `.venv`, then run `.venv/bin/python -m pytest tests/test_input_parser.py tests/test_proxies.py tests/test_models.py -q`. Expected initial failure: package/function missing, not an unrelated interpreter error.
- [ ] **4. Implement contracts and parsing.** Use `text.removeprefix("\ufeff").splitlines()`; retain the password slice exactly; collect safe line failures instead of aborting a batch. Use Base32 decoding/TOTP construction to validate the normalized secret; email validation rejects obvious malformed input without alias rewriting. Normalize supported proxy formats into one model, check ports 1–65535, reject unsupported schemes and blank hosts. Invalid proxy input raises `FlowError("CONFIGURATION_ERROR", "proxy_parse")`; no fallback-to-direct branch.

```python
parts = raw_line.split("|")
if len(parts) != 3 or any(not item.strip() for item in parts):
    rejected.append(RejectedLine(line_number, "INVALID_FIELDS", "Expected three non-empty fields"))
    continue
email, password, raw_secret = parts
email = email.strip()
secret = raw_secret.replace(" ", "").replace("-", "").upper()
```

- [ ] **5. Verify green and commit.** Run the three test files and Ruff. Initialize dependency bounds based on the tested versions, record them in `pyproject.toml`, and include private data/venv/dependency checkouts in `.gitignore`. Commit only this task's files with `feat: add validated account and proxy inputs`.

## Task 2: PKCE, callback lifecycle, guarded transport and validated token export

**Files:** Create `oauth.py`, `tokens.py`, `transport.py`; create `tests/test_oauth.py`, `tests/test_tokens.py`, `tests/test_transport.py`, `tests/fakes.py`.

**Interfaces:** Consume Task 1 types. Produce `OAuthSessions(settings, clock=time.monotonic)`, `.create(expected_email: str, proxy: ProxyConfig | None) -> OAuthSession`, `.claim(session_id: str, callback_url: str) -> AuthorizationGrant`, `.discard(session_id: str) -> None`. `OAuthSession` exposes `session_id`, `desktop_url`, `authorize_url`, `expires_at`; verifier is repr-hidden/backend-only. `AuthorizationGrant` contains expected email, hidden code/verifier, redirect URI and proxy. Produce `SafeTransport(proxy, context, raw_session=None).request(method, url, **kwargs)`, `.close()`; `TokenService.complete(grant, transport) -> SuccessRecord`.

- [ ] **1. Write failing lifecycle tests** using a fixed clock and controlled random generator only in tests; never reuse fixed production state/verifiers. Cover nested URL parsing, all parameters from the spec, unique states, expiry, foreign state, malformed/duplicate query parameters, denied callbacks, wrong callback host/path/port, replay, and two threads racing to claim one session.

```python
from urllib.parse import parse_qs, urlsplit
import pytest
from autobuild_json.errors import FlowError
from autobuild_json.oauth import OAuthSessions

def test_nested_url_and_single_use(settings):
    sessions = OAuthSessions(settings, clock=lambda: 100.0)
    item = sessions.create("u@example.com", None)
    outer = parse_qs(urlsplit(item.desktop_url).query)
    inner = parse_qs(urlsplit(outer["authorize_url"][0]).query)
    assert outer["no_universal_links"] == ["1"]
    assert inner["redirect_uri"] == ["http://localhost:1455/auth/callback"]
    assert inner["code_challenge_method"] == ["S256"]
    callback = "http://localhost:1455/auth/callback?code=fake&state=" + inner["state"][0]
    sessions.claim(item.session_id, callback)
    with pytest.raises(FlowError) as error:
        sessions.claim(item.session_id, callback)
    assert error.value.code == "INVALID_STATE"
```

Define `settings(tmp_path)` in `conftest.py`: temporary private data path, fixed fake install UUID and admin token; no real dependency path required for pure OAuth tests. Add a request-count test proving a timed-out token POST is attempted exactly once. Build ephemeral RSA keys with `cryptography` in a `signed_tokens` fixture; sign JWTs with known email/account claims and vary signature, audience, issuer, expiry, missing claims, missing refresh token and email mismatch. Do not decode or copy the user's real token file into tests.

- [ ] **2. Verify red.** Run `.venv/bin/python -m pytest tests/test_oauth.py tests/test_tokens.py tests/test_transport.py -q`; confirm the planned symbols/behavior are missing.
- [ ] **3. Implement link/session lifecycle.** Use CSPRNG and standard URL encoders; protect the state registry with a lock, perform validation and consumption atomically, and expire entries before enforcing the 100-session cap. Invalid foreign callbacks must not consume a valid pending session; once valid redemption starts, never allow replay even after an ambiguous network failure.

```python
verifier = secrets.token_urlsafe(48)
state = secrets.token_urlsafe(32)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
authorize_url = "https://auth.openai.com/oauth/authorize?" + urllib.parse.urlencode(params)
desktop_url = "https://chatgpt.com/codex/desktop-auth?" + urllib.parse.urlencode({
    "authorize_url": authorize_url,
    "codex_streamlined_login": "true",
    "no_universal_links": "1",
})
```

`params` contains exactly the spec's authorize fields, generated state/challenge, configured version and shared stable installation UUID. Persist the UUID once with private atomic configuration, not once per account.

- [ ] **4. Implement transport and token verification.** Disable automatic redirects; verify TLS; check deadline/cancellation before every request. Limit accepted bodies to 2 MiB using a bounded streaming/write callback, including error bodies. Allow only required fixed HTTPS auth/chatgpt/sentinel origins and known operation routes, with no URL userinfo/nonstandard ports. Token and JWKS requests use only the fixed issuer origin; never follow token-header `jku`/`x5u`. Cache trusted JWKS briefly under a lock, refetch once for an unknown `kid`, and fail closed if discovery/JWKS cannot be verified. Preserve the selected proxy even for trust-metadata fetches.

```python
response = transport.request("POST", "https://auth.openai.com/oauth/token", data={
    "grant_type": "authorization_code",
    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
    "code": grant.code,
    "code_verifier": grant.verifier,
    "redirect_uri": grant.redirect_uri,
}, allow_redirects=False)
claims = jwt.decode(
    payload["id_token"], trusted_key, algorithms=["RS256"],
    audience="app_EMoamEEZ73f0CkXaXp7hrann", issuer="https://auth.openai.com",
    options={"require": ["exp", "iat", "iss", "aud", "sub"]},
)
```

Here `payload` is the validated JSON object from `response`, and `trusted_key` is obtained exclusively through the bounded trusted JWKS loader in `tokens.py`. Reject missing/empty three-token values, unverified email, missing account ID and mismatched expected email before creating a `SuccessRecord`. Use typed network errors, never raw response text, and emit `TOKEN_EXCHANGE_UNCERTAIN` after ambiguous POST failure.

- [ ] **5. Verify green and commit.** Run the three test modules plus Task 1 tests. Commit `feat: add isolated PKCE sessions and token validation`.

## Task 3: Integrate CheckLive HTTP auth with safe challenge handling

**Files:** Create `checklive_adapter.py`, `challenges.py`, `navigation.py`, `scripts/setup_checklive.py`, `THIRD_PARTY.md`; create `tests/test_checklive_adapter.py`, `tests/test_navigation.py`, `tests/test_dependency_setup.py`.

**Interfaces:** Produce `load_checklive(path: Path) -> ModuleType`, `detect_challenge(payload: object, url: str) -> str | None`, `CheckliveProcessor(settings, oauth_sessions, token_service)` implementing the shared `Processor` callable. `OAuthNavigator(transport, context).complete(authorize_url: str) -> str` returns the intercepted callback URL without requesting localhost.

- [ ] **1. Write failing auth/navigation tests.** Model a small fake `AuthSession` exposing the actual pinned methods: `visit_homepage`, `get_csrf_token`, `get_auth_url`, `init_oauth`, `authorize_continue`, `verify_password`, `mfa_issue_challenge`, `mfa_verify`, continuation helpers and `.session.close`. Record method order and proxy identity; inject phone/email OTP/error responses at every stage. Include network/invalid-state retries, deadline, cleanup, blocked external redirect, unsupported consent HTML and ambiguous workspaces.

```python
from autobuild_json.challenges import detect_challenge

def test_phone_challenge_is_not_a_profile_field():
    assert detect_challenge({"phone_number": None}, "https://auth.openai.com/profile") is None
    assert detect_challenge({"page": {"type": "phone_verification"}}, "https://auth.openai.com/add-phone") == "PHONE_VERIFY"
    assert detect_challenge({"page": {"type": "mfa_totp"}}, "https://auth.openai.com/mfa-challenge/abc") is None

def test_callback_is_never_requested(fake_transport, context):
    from autobuild_json.navigation import OAuthNavigator
    fake_transport.queue_redirect("http://localhost:1455/auth/callback?code=c&state=s")
    result = OAuthNavigator(fake_transport, context).complete("https://auth.openai.com/oauth/authorize?state=s")
    assert result.startswith("http://localhost:1455/auth/callback?")
    assert len(fake_transport.requests) == 1
```

Define `FakeTransport` in `tests/fakes.py` with `.requests`, `.queue_redirect(url)`, `.queue_json(status, payload)`, `.request(...)`, `.close()` and no socket access; fixtures construct a fresh instance per test. Assert `caplog` and serialized failures never contain seeded password/secret/proxy/token strings, even when upstream raises an exception containing them. Test a missing dependency and incompatible method signature as safe startup errors.

- [ ] **2. Verify red.** Run `.venv/bin/python -m pytest tests/test_checklive_adapter.py tests/test_navigation.py tests/test_dependency_setup.py -q`.
- [ ] **3. Implement dependency preparation and guarded integration.** Setup clones only into a new ignored dependency directory or uses an explicitly selected existing path; if a target exists, validate it without overwriting/resetting it. Checkout the fixed commit only in the newly cloned directory, then verify `rev-parse HEAD`. Do not run the upstream CLI or import optional sibling solver code. Record URL/commit and upstream license status (no explicit license was detected) without claiming the code is relicensed.

```python
AUTH_REPO = "https://github.com/tmq9999/Check-Account-ChatGPT.git"
AUTH_COMMIT = "791beb350370c191bbe8ddbe0b4a3fa0072620d9"
required_methods = (
    "visit_homepage", "get_csrf_token", "get_auth_url", "init_oauth",
    "authorize_continue", "verify_password", "mfa_issue_challenge", "mfa_verify",
)
if not all(callable(getattr(module.AuthSession, name, None)) for name in required_methods):
    raise FlowError("CONFIGURATION_ERROR", "dependency_check")
```

Prevent the optional solver import before loading upstream `sentinel`: load the pinned `checklive.config` first, override only its `WORKSPACE_ROOT` once at startup to an application-owned empty 0700 directory, then import `checklive.auth`. Verify that no `aBaiAutoplus` entry or symlink exists in that private directory, and reject an already-imported incompatible `checklive` namespace. This confines the pinned optional-import path without scanning/loading unrelated sibling projects. Test it with a fake sentinel VM marker file in the reference checkout's real sibling path and assert the marker never executes. Do not change these module settings per worker.

Create a subclass/adapter whose `_request` delegates to the guarded transport and whose `_debug` is a no-op. Wrap both `.request` and `.post` used by upstream Sentinel so they enforce proxy, timeout, host, response-size and cancellation policy; stop with `ACTION_REQUIRED` if response requires CAPTCHA/Turnstile. Bound or stop any unsupported upstream challenge work rather than allowing it to exceed the attempt deadline. Do not call the legacy retrying `_request` recursively.

- [ ] **4. Implement the minimal state machine.** Drive the pinned email/password/TOTP methods explicitly to inspect each response. Do not call subscription APIs or treat `login()`'s ChatGPT access token as a Codex token. Recognize phone challenge only from relevant page/error/redirect markers, map wrong TOTP to `MFA_ERROR` rather than bad password, and reject email OTP/blocked flows. Preserve session cookies through Codex authorization. Select a sole authenticated account/workspace only; multiple unresolved choices become `WORKSPACE_SELECTION_REQUIRED`. Intercept/claim callback and call Task 2 token exchange on the same transport.

```python
try:
    context.check()
    auth.visit_homepage()
    csrf = auth.get_csrf_token()
    login_url = auth.get_auth_url(account.email, csrf)
    auth.init_oauth(login_url)
    email_response = auth.authorize_continue(account.email)
    challenge = detect_challenge(email_response, login_url)
    if challenge:
        raise FlowError(challenge, "email")
    password_response = auth.verify_password(account.password)
finally:
    transport.close()
```

The complete method continues through challenge extraction/issuance/TOTP/continuation and Codex navigation inside this same `try`, keeping `finally` around the entire attempt. Cache no account credentials on the processor object. A permitted `invalid_state` restart constructs a new session and new PKCE/state, once; `403`/`429` do not trigger proxy switching or repeated login.

- [ ] **5. Verify green and commit.** Run auth/navigation/setup tests and all earlier tests. Commit `feat: integrate pinned HTTP auth with classified failures`.

## Task 4: Durable results and exact export schema

**Files:** Create `results.py`; create `tests/test_results.py`.

**Interfaces:** Produce `RunStore(root: Path)`, `.create(report: ParseReport) -> str`, `.update(job_id, account_job_id, *, status, stage, attempt, result: RunResult | None = None) -> None`, `.snapshot(job_id) -> dict`, `.export(job_id, kind: str) -> bytes`, `.recover_interrupted() -> None`. `kind` is one of `success`, `errors`, `phone_verify`, `filtered`, `cancelled`.

- [ ] **1. Write failing persistence tests.** Test exact key sets, empty arrays, no input secrets, private mode bits on POSIX, forbidden export kind/path traversal, concurrent updates and disk-write failure. Simulate restart by constructing a second store over the same temporary directory and calling recovery. Build `success_result()` in `tests/fakes.py` from synthetic `SuccessRecord` tokens, never from the user's JSON.

```python
import json
from autobuild_json.input_parser import parse_accounts
from autobuild_json.results import RunStore
from tests.fakes import success_result

def test_export_and_restart_keep_confirmed_success(tmp_path):
    report = parse_accounts("u@example.com|private-pass|JBSWY3DPEHPK3PXP")
    store = RunStore(tmp_path)
    job_id = store.create(report)
    store.update(job_id, report.accounts[0].account_job_id, status="success", stage="complete", attempt=1, result=success_result())
    reopened = RunStore(tmp_path)
    reopened.recover_interrupted()
    row = json.loads(reopened.export(job_id, "success"))[0]
    assert set(row) == {"id", "email", "account", "tokens"}
    assert set(row["tokens"]) == {"id_token", "access_token", "refresh_token"}
    assert "private-pass" not in repr(reopened.snapshot(job_id))
```

- [ ] **2. Verify red.** Run `.venv/bin/python -m pytest tests/test_results.py -q`.
- [ ] **3. Implement one authoritative atomic snapshot per batch.** Keep records/status/errors in one private `run.json` to avoid inconsistencies between separately committed files. Export arrays are generated from that snapshot in original input order. Build public snapshots with an allowlist of fields and no token records. Update an in-memory snapshot only after successful persistence, under a per-batch lock; on failure retain the last durable state and raise safe `STORAGE_ERROR` for the runner.

```python
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(snapshot, handle, ensure_ascii=False)
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary_path, snapshot_path)
```

Create `fd, temporary_path` with `tempfile.mkstemp` inside the private batch directory and mode 0600. Fsync the parent directory where supported. Validate generated job IDs before resolving paths; never follow a user-supplied path. Recovery converts persisted queued/running rows to cancelled with `Interrupted by application restart`; successful records remain unchanged. Include line IDs/email/stage/attempt/time in safe failures, not account raw strings.

- [ ] **4. Verify green and commit.** Run result tests and earlier suites. Commit `feat: persist private atomic result snapshots and exports`.

## Task 5: Bounded batch runner, sticky proxies and cancellation

**Files:** Create `runner.py`; create `tests/test_runner.py`.

**Interfaces:** `Runner(store: RunStore, processor: Processor)` provides `.start(report, proxies, mode: str, workers: int, timeout: float) -> str`, `.get(job_id) -> dict`, `.stop(job_id) -> None`, `.wait(job_id, timeout: float) -> None`, `.close() -> None`. `.wait()` exists for deterministic tests/CLI cleanup, not to block async API handlers.

- [ ] **1. Write failing concurrency tests.** Use `threading.Event`/barriers and counters instead of long sleeps. Check global worker bound, effective proxy bound, same-email exclusion, round-robin assignment including sequential runs, no cross-account state sharing, a second active batch rejected, Stop pending/running behavior, per-account deadline, store failure and session cleanup.

```python
import threading
from autobuild_json.input_parser import parse_accounts
from autobuild_json.models import RunResult
from autobuild_json.runner import Runner

def test_stop_cancels_remaining_accounts(store):
    entered, release = threading.Event(), threading.Event()
    def processor(account, proxy, context):
        entered.set()
        assert release.wait(2)
        context.check()
        return RunResult(status="cancelled")
    report = parse_accounts("a@example.com|p|JBSWY3DPEHPK3PXP\nb@example.com|p|JBSWY3DPEHPK3PXP")
    runner = Runner(store, processor)
    job_id = runner.start(report, [], "sequential", 1, 180)
    assert entered.wait(2)
    runner.stop(job_id)
    release.set()
    runner.wait(job_id, timeout=3)
    assert runner.get(job_id)["counts"]["cancelled"] == 2
```

Define `store(tmp_path)` in `conftest.py` using Task 4. Add a processor spy that returns success while `RunStore.update` is patched to raise on persistence: verify batch reports storage failure, schedules no more accounts, and never increments a durable success count falsely.

- [ ] **2. Verify red.** Run `.venv/bin/python -m pytest tests/test_runner.py -q`.
- [ ] **3. Implement a dedicated coordinator plus bounded `ThreadPoolExecutor`.** Reserve email/proxy identities in the coordinator before submitting work, avoiding worker starvation from tasks waiting on locks. Process the input queue once, assigning the next available proxy in round-robin order. Use at most `workers` active futures (`1` in sequential mode); never enqueue thousands of credential-carrying futures. Keep each account's credentials only until its future finalizes, then drop references. With no proxy the only limits are workers and same-email exclusion.

```python
context = AttemptContext(
    deadline=time.monotonic() + timeout,
    cancel=batch_cancel,
    on_stage=lambda stage, attempt, account_job_id=account.account_job_id: store.update(job_id, account_job_id, status="running", stage=stage, attempt=attempt),
)
future = executor.submit(processor, account, proxy, context)
```

Capture account IDs by value in callbacks, not late-bound loop variables. Classify raised `FlowError` including phone/cancel/timeout into one terminal result; map unexpected failures to a generic safe error without retaining traceback credentials. Persist terminal result before releasing it to polling. On storage failure cancel the batch and expose a sanitized batch-level `STORAGE_ERROR` in memory while preserving disk's last confirmed snapshot. `.close()` signals cancellation and bounds shutdown by request timeout.

- [ ] **4. Verify green and commit.** Repeat race tests with `.venv/bin/python -m pytest tests/test_runner.py -q` several times, then run the whole suite. Commit `feat: run bounded OAuth batches with cooperative cancellation`.

## Task 6: Protected FastAPI endpoints and manual OAuth completion

**Files:** Create `security.py`, `api_models.py`, `api.py`, `__main__.py`; create `tests/test_api.py`, `tests/test_security.py`, `tests/test_manual_oauth.py`.

**Interfaces:** `create_app(settings: Settings, runner: Runner | None = None, oauth_sessions: OAuthSessions | None = None, token_service: TokenService | None = None) -> FastAPI`. Tests inject fakes. Production lifespan creates services, checks dependency readiness, recovers interrupted batches and closes runner on shutdown. Implement all routes from spec plus `POST /api/session` and `DELETE /api/session` for local admin authentication.

- [ ] **1. Write failing API/security tests.** Test unauthorized export, correct bootstrap-token exchange, HttpOnly/SameSite cookie, constant-time token checks, state-changing CSRF, origin/host restrictions, validation redaction, oversized chunked request rejection, unknown job IDs, simultaneous Start conflict, no tokens in polling, no query parameters logged, cleanup on lifespan exit and injected fake OAuth end-to-end. API models use `SecretStr` for account text/proxy text/bootstrap token/callback URL and translate them to internal types only inside the boundary.

```python
def test_validation_errors_never_echo_input(authenticated_client):
    client, csrf = authenticated_client
    response = client.post("/api/jobs", headers={"X-CSRF-Token": csrf}, json={
        "accounts_text": "u@example.com|private-pass|PRIVATE-SECRET",
        "proxies_text": "http://u:proxy-password@127.0.0.1:8080",
        "mode": "parallel", "workers": "private-worker-value", "timeout": 180,
    })
    assert response.status_code == 422
    for secret in ("private-pass", "PRIVATE-SECRET", "proxy-password", "private-worker-value"):
        assert secret not in response.text

def test_export_requires_session(client):
    assert client.get("/api/jobs/00000000-0000-4000-8000-000000000001/exports/success").status_code == 401
```

Define `client` as a TestClient context with a fake processor and fake token service. `authenticated_client` posts the synthetic bootstrap token to `/api/session`, obtains a CSRF token, and sets the matching allowed Origin. Add tests that manually create a link and redeem the same callback twice; exactly one token-service invocation is allowed.

- [ ] **2. Verify red.** Run `.venv/bin/python -m pytest tests/test_api.py tests/test_security.py tests/test_manual_oauth.py -q`.
- [ ] **3. Implement security before wiring credential routes.** Read/bootstrap admin token from a private config source, never URL queries. Exchange it for random server-side session ID and CSRF secret; rotate on login, expire after 8 hours, revoke on logout. Cookie is HttpOnly, SameSite=Strict, Secure when HTTPS. Deny foreign Host/Origin; require CSRF on mutation even for local clients. Protect API docs/openapi or disable them by default. Serve only the login shell/static assets unauthenticated. Do not expose dependency paths in `/api/health`.

```python
@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    fields = [{"field": ".".join(map(str, item["loc"])), "code": item["type"]} for item in exc.errors()]
    return JSONResponse(status_code=422, content={"error": "INVALID_REQUEST", "fields": fields}, headers={"Cache-Control": "no-store"})
```

Sanitize location components too if they can originate from user-controlled dictionary keys; schemas should reject arbitrary extra dict structures. Use bounded ASGI request-body consumption (5 MiB decoded account text and a documented JSON envelope limit of 32 MiB to cover escaped Unicode) rather than trusting Content-Length. API error responses never include `.errors()` raw `input`, request bodies or exception messages.

- [ ] **4. Wire endpoints without blocking the event loop.** Run blocking token/dependency/storage work through FastAPI threadpool helpers; runner start returns immediately with job ID. Copy only sanitized progress fields into responses. Make export kind an enum and send attachment/no-store headers. For manual OAuth, create expected-email sessions without network login, claim state atomically and return the success array as a JSON attachment; never append it to activity logs. Build accounts from fresh validation at Start, not from client preview IDs. No credentials in GET queries, localStorage or API URL paths.

```python
return Response(
    content=store.export(job_id, kind.value),
    media_type="application/json",
    headers={"Content-Disposition": f'attachment; filename="{kind.value}.json"', "Cache-Control": "no-store"},
)
```

Launcher binds configured localhost port (default 8787), one worker, no reload and query-free access logging. If auth dependency is missing, allow UI/parser/manual-link generation to load but reject batch Start with a safe actionable `CONFIGURATION_ERROR`; distinguish app health from auth readiness.

- [ ] **5. Verify green and commit.** Run API/security/manual tests and all earlier suites. Commit `feat: expose protected batch and OAuth APIs`.

## Task 7: Vietnamese local dashboard and UI acceptance checks

**Files:** Create `autobuild_json/static/index.html`, `autobuild_json/static/app.js`, `autobuild_json/static/style.css`, `package.json`; create `tests/test_static_ui.py`, `tests/ui/state.test.mjs`, `tests/ui/acceptance.md`. Include static assets as package data in `pyproject.toml`.

**Interfaces:** UI consumes only Task 6's documented routes and public DTOs. Export `summarizeRows(rows)`, `shouldPoll(job, hidden)`, and `displayReason(row)` from `app.js` so Node's built-in test runner can exercise pure state logic without a bundled framework.

- [ ] **1. Prepare the visual design through Superdesign.** Read its skill and required reference instructions at this stage; use the new-project route and a synthetic brief only. No real emails, `json.txt`, tokens, dependency captures or auth source go to external design context. Request one desktop-first Vietnamese utility dashboard: account import on left, execution settings on right, metrics and result table below, compact manual OAuth panel. Use accessible contrast/focus states and text status labels, not color alone. Follow its login/blocker and user-review rules; do not claim a canvas draft exists if generation is unavailable. Persist the approved design direction and canvas link in local design notes.

- [ ] **2. Write failing UI contract/state tests.** Check labeled file/paste inputs, Start/Stop disabled states, all five export kinds, manual callback controls, local-only assets, escaped email/error display, and poll cancellation on hidden/end state.

```javascript
import test from 'node:test';
import assert from 'node:assert/strict';
import { summarizeRows, shouldPoll } from '../../autobuild_json/static/app.js';

test('counts phone verification separately', () => {
  const counts = summarizeRows([{status:'success'}, {status:'phone_verify'}, {status:'error'}]);
  assert.equal(counts.success, 1);
  assert.equal(counts.phone_verify, 1);
  assert.equal(counts.error, 1);
});

test('stops polling when hidden or finished', () => {
  assert.equal(shouldPoll({status:'running'}, true), false);
  assert.equal(shouldPoll({status:'completed'}, false), false);
  assert.equal(shouldPoll({status:'running'}, false), true);
});
```

Create root dev-only `package.json` with `{"private":true,"type":"module","scripts":{"test:ui":"node --test tests/ui/*.test.mjs"}}` so the imported `autobuild_json/static/app.js` is treated as an ES module too. This is test tooling only, not a runtime installation requirement. Make `app.js` importable without touching `document` until an explicit `mountDashboard()` call.

- [ ] **3. Verify red.** Run `node --test tests/ui/*.test.mjs` and `.venv/bin/python -m pytest tests/test_static_ui.py -q`.
- [ ] **4. Implement dashboard behavior.** UI reads UTF-8 files locally with size checks and `new TextDecoder('utf-8', {fatal:true}).decode(await file.arrayBuffer())` so invalid UTF-8 is rejected instead of silently changed; sends text to validate/Start endpoints; clears password-bearing text fields once a job is accepted. Do not store inputs in browser storage. Put user/provider text into `textContent`, never raw `innerHTML`. Render masked import preview and safe account status rows. Poll once per second only while running/visible; avoid overlapping requests and abort on logout/navigation. Fetch exports as blobs and revoke object URLs after download. Keep tokens only in the explicit download response and never in DOM/logs.

```javascript
export function shouldPoll(job, hidden) {
  return !hidden && ['queued', 'running', 'stopping'].includes(job.status);
}

export function summarizeRows(rows) {
  const counts = {queued:0, running:0, success:0, error:0, phone_verify:0, cancelled:0};
  for (const row of rows) if (Object.hasOwn(counts, row.status)) counts[row.status] += 1;
  return counts;
}
```

Display the exact `Phone number verify` label and Vietnamese explanation. Show dependency-not-ready and storage-failure errors plainly. Manual flow asks expected email, generates/copies desktop link, explains the localhost error/paste-callback behavior and downloads completed JSON. Do not open/login accounts automatically on page load.

- [ ] **5. Verify green and review actual rendered UI.** Run Node/Python tests; launch a fake-backend local server and exercise login, mixed input, sequential/parallel settings, phone/error rows, Stop, download and narrow viewport. Use available browser tooling for interaction/screenshots; if unavailable, record manual acceptance as unverified rather than claiming visual/e2e validation. Commit `feat: add local Vietnamese OAuth dashboard`.

## Task 8: Reproducible setup, whole-system verification and handoff

**Files:** Create `README.md`, `.env.example`; update `pyproject.toml`, `tests/conftest.py`, `tests/test_end_to_end.py`, `tests/test_setup.py`, `tests/ui/acceptance.md` and task checkboxes.

**Interfaces:** Document installation, dependency commit verification, local admin bootstrap, start command, privacy model, input/proxy formats, output schema, Stop/restart semantics, manual callback flow and unsupported challenges. No automatic live-auth smoke test is part of setup.

- [ ] **1. Write final failing integration checks.** Global test fixtures deny outbound network unless the individual test explicitly provides an in-memory transport. Exercise authenticated UI/API workflow with fake account outcomes covering success/error/phone/cancel; check downloaded arrays, no secret leakage, restart recovery and single-process batch conflict. Install/build the package in an isolated test environment and ensure static assets are included.

```python
def test_mixed_batch_exports_have_exact_terminal_counts(api_scenario):
    scenario = api_scenario(outcomes=["success", "error", "phone_verify"])
    scenario.start_and_wait()
    assert len(scenario.download("success")) == 1
    assert len(scenario.download("errors")) == 1
    assert scenario.download("phone_verify")[0]["reason"] == "Phone number verify"
    assert scenario.download("cancelled") == []
    assert "refresh_token" not in scenario.public_status_text()
```

Define `api_scenario` in `conftest.py` as a factory around TestClient, synthetic account text, fake processor outcomes, admin login and bounded polling; its methods call real app routes. No monkeypatch bypass of production security dependency is allowed in this acceptance fixture.

- [ ] **2. Run red tests, finish integration/docs, then run verification.** Publish commands that work from the new project and use its venv; provide Windows equivalents where shell syntax differs. Keep setup checkout ignored and do not silently replace an existing user's checkout. Never imply the generated example tokens are usable.

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m compileall -q autobuild_json
node --test tests/ui/*.test.mjs
.venv/bin/python -m build
git diff --check
git status --short
```

Add `build` as a development dependency for the packaging check. Review build output so it excludes data, dependency checkout, `.env`, accounts and token artifacts. Repeat security/race/storage tests after integration changes.

- [ ] **3. Perform required independent review and address findings.** Use `requesting-code-review` under the chosen execution workflow; reviewers get the spec, plan and actual diff. Focus on the five Review Focus items, transport wrappers bypassed by upstream methods, code redemption replay, secret-bearing exceptions/tracebacks, rate limits, disk failure and shutdown. Read `receiving-code-review` before implementing review suggestions; use `systematic-debugging` for actual failures. Do not mark a test passed based on reviewer speculation.
- [ ] **4. Run fresh final verification and hand off.** Read `verification-before-completion`; rerun the relevant commands and record actual outcomes. Use the finishing skill where applicable, without pushing/merging externally unless the user requests it. State the startup command, local URL, dependency readiness and offline verification results. Explicitly state that live OAuth remains unverified unless the user separately authorized and supplied a specific account for a real run. Do not expose real credentials in the final response.

## Execution handoff

Recommended method: **Native** — one implementer in this session, tasks in dependency order, then an independent whole-project review. The eight tasks share tight contracts (session ownership, safe DTOs and persistence), so this avoids repeating integration context across multiple implementers. **Subagent-driven** is the alternative for independent per-task implementation/review gates at higher context cost.

Do not start product implementation until the user has reviewed this plan and selected the method. No product code, dependencies or auth checkout have been created by this planning step.
