# Multi-protocol API Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing tool with an independently written multi-customer API gateway, provider resale configuration, weighted-token quotas, Codex OAuth and four proxy modes.

**Architecture:** A modular FastAPI gateway has its own listener, separate from the existing private OAuth/admin application. PostgreSQL is the authority for policy, encrypted credentials, quota reservations, leases and immutable usage. Inbound protocol codecs and outbound provider adapters communicate through typed events; all protocols share the same accounting pipeline.

**Tech Stack:** Existing Python >=3.10/FastAPI/Pydantic/Uvicorn; PostgreSQL 17; SQLAlchemy 2 async sessions with psycopg 3; Alembic migrations; HTTPX/httpcore for gateway HTTP; cryptography AES-GCM; existing curl_cffi for legacy OAuth. Plain JavaScript/CSS admin UI, pytest/pytest-asyncio, Node tests, existing Chrome/Playwright-core development harness.

**Spec:** `docs/superpowers/specs/2026-09-24-api-gateway-design.md` — approved by the user's “triển khai đi” after receiving commit `4b7872f`. Plan and recommended Native execution approved by “duyệt” after commit `e409a8f`; execution began in isolated branch `feat/api-gateway`.

## Global Constraints

- Codebase riêng trong project hiện tại; không sao chép source/UI/sidecar của FreeLLMAPI, Cockpit hoặc 9router.
- “Không yêu cầu Redis ở bản đầu”; PostgreSQL transactions, row locks and fencing protect shared state.
- “Secret ngẫu nhiên tối thiểu 256 bit”; client secrets are shown once and stored as digests, upstream secrets are encrypted.
- “1 token quy đổi = 1.000.000 đơn vị”; fixed-point accounting, maximum six coefficient decimals, no floats.
- Quota formula: `input_tokens × hệ số input + output_tokens × hệ số output`; override replaces, never multiplies the model coefficient.
- “Null là không giới hạn, 0 là không còn quota”; daily/monthly windows use UTC admission time.
- “Tối đa hai attempt generation”; never replay uncertain generation or switch after committing a client response.
- “Body gateway tối đa 16 MiB”; “SSE frame 1 MiB”; “tổng tool arguments 1 MiB/request”; “số tool calls tối đa 128”.
- Request wall-clock 180 seconds by default, maximum 600; connect 10 seconds; stream idle 60 seconds.
- Kiot acquisition maximum 10 seconds; operation deadline + 15 seconds grace before rotation after lost ownership.
- Direct/fixed/pool/KiotProxy; one Kiot key per line, one active operation per key; no automatic direct fallback or `/out`.
- Continuation and optional duplicate-request claims live 24 hours; usage holds do not expire merely because worker leases do.
- Gateway port 8788 loopback by default; current OAuth/admin port and default launch behavior remain unchanged.
- Four inbound families: OpenAI, Anthropic, Gemini, Ollama; five outbound adapters including Codex OAuth.
- No payment processor, embeddings, media generation, remote tool execution, arbitrary HTTP proxy or WebSocket in this release.
- Never read real `account.txt`, `data/local-config.json`, `success.json` or reference `json.txt` into tests, plans, logs or commits.
- Never public-bind/restart the user's existing server without checking active work; do not alter client Codex profiles or firewall rules.

## Review Focus

1. A stream loses its final usage event: preserve its hold, cancel safely, emit no fake success (Tasks 4, 10, 18).
2. Key rotation/config edits occur during requests: preserve one policy/coefficient snapshot, revoke old secrets for new admission (Tasks 3, 4, 5).
3. A hostname resolves to public then private IP, including through a proxy: validate actual connection and document remote-DNS enforcement limits (Tasks 6, 19).
4. OAuth refresh or Kiot allocation times out after being accepted: do not replay mutating grants/allocations or rotate another worker's IP (Tasks 7, 15).
5. Multiple protocol streams contain interleaved partial tool arguments and cache-inclusive usage: preserve IDs, framing and one charge without double-counting (Tasks 8–14).

Each item has an explicit test below. Passing unit tests does not substitute for PostgreSQL concurrency or real SDK compatibility tests.

## Execution conditions and milestones

Repository: `/home/mquangprovip0503/Work/Code/AutoBuildJsonCockplit-Tools`.
Planning baseline: commit `4b7872f` on `feat/http-oauth`, initially clean.
Use `.venv/bin/python`, not system Python. Create/verify an isolated worktree with
`superpowers:using-git-worktrees` only after execution is approved; preserve user data.

At planning time `docker`, `podman`, `psql`, `pg_ctl` and `initdb` are not in PATH.
Task 1 includes a test-only PostgreSQL setup path; do not claim database tests pass
if no test server exists. Do not install a system service or connect to a production
database implicitly. A missing database blocks DB verification, not pure-code work.

- Milestone A, Tasks 1–10: one real OpenAI-compatible route, key auth and accounting.
- Milestone B, Tasks 11–16: remaining protocols, provider registry, OAuth, all proxy modes.
- Milestone C, Tasks 17–19: admin UI, operations, SDK matrix, regression and handoff.

All three milestones belong to the approved first release. Each task is independently
reviewable and must run RED → GREEN → focused regression before its commit. Each
numbered implementation action below is a short step; repeat the test loop for each
added failure case, rather than implementing an entire milestone before testing.

Commit only paths owned by the task. If Git author configuration is absent, use
command-local `-c user.name=Codex -c user.email=codex@localhost`, never change global config.

## File map and contract ownership

The package is `autobuild_json/gateway/`; existing OAuth modules retain their names.
`gateway/` in task file lists is shorthand for `autobuild_json/gateway/`, not a
second root-level package. Every new package directory gets `__init__.py`. Keep modules focused; split codecs
into decode/encode/stream files when their responsibilities grow.

| Path below `autobuild_json/gateway/` | Responsibility / owning task |
|---|---|
| `settings.py`, `storage/db.py`, `storage/schema.py`, `storage/migrations/` | Optional service setup/migrations, 1 |
| `secrets.py`, `errors.py`, `contracts.py` | Secret formats, safe errors, typed shared records, 2/8 |
| `identity/service.py`, `identity/policy.py` | Customers, keys, allowlists and audit, 3 |
| `metering/units.py`, `metering/ledger.py`, `metering/costs.py` | Reservation, settlement and independent cost accounting, 4 |
| `providers/catalog.py`, `routing/select.py`, `routing/continuations.py` | Provider/model CRUD, selection, scoped handles, 5/11 |
| `transport/egress.py`, `transport/http.py`, `transport/network.py` | Root validation and connection-time egress, 6 |
| `proxy/config.py`, `proxy/leases.py`, `proxy/kiot.py`, `proxy/manager.py` | Profiles, fenced leases and Kiot lifecycle, 7 |
| `protocols/frames.py`, `protocols/common.py` | Incremental parsers and normalized events, 8 |
| `providers/openai.py`, `protocols/openai_chat.py`, `protocols/openai_responses.py` | OpenAI wire contracts, 9/11 |
| `http/app.py`, `http/auth.py`, `http/boundary.py`, `http/streaming.py`, `engine.py` | Public app and shared request lifecycle, 10 |
| `protocols/anthropic.py`, `providers/anthropic.py` | Anthropic wire contracts, 12 |
| `protocols/gemini.py`, `providers/gemini.py` | Gemini wire contracts, 13 |
| `protocols/ollama.py`, `providers/ollama.py` | Ollama wire contracts, 14 |
| `accounts/imports.py`, `accounts/refresh.py`, `providers/codex.py` | Token import, refresh and Codex protocol, 15 |
| `proxy/legacy.py` | Existing runner/HTTP boundary integration, 16 |
| `admin/routes.py`, `admin/schemas.py`, `admin/usage.py`, `admin/static/` | Private admin APIs/UI, 17 |
| `maintenance.py`, `storage/backup.py`, `__main__.py` | Recovery, retention, backup, opt-in service launch, 18 |

Tests live in `tests/gateway/`, with `unit/`, `integration/`, `sdk/` subdirectories;
UI tests stay in `tests/ui/`. Provider payloads are synthetic fixtures in
`tests/gateway/fixtures/`, never copied source or real captures with credentials.

### Shared public signatures

Use these names consistently; the owning task supplies implementation and complete
validation. Datetimes are timezone-aware UTC. UUIDs are application IDs, not secrets.

| Owner | Public contract |
|---|---|
| 1 | `make_database(url: SecretStr) -> Database`; `Database.sessions` is an `async_sessionmaker`; `await Database.close()` |
| 2 | `issue_key() -> tuple[str, str]`; `key_digest(secret: str, pepper: bytes) -> bytes`; `Vault.seal(kind: str, record_id: UUID, value: bytes) -> Ciphertext`; `Vault.open(kind: str, record_id: UUID, value: Ciphertext) -> bytes` |
| 3 | `IdentityService(db, pepper)`; async `create_customer(name)`, `create_key(customer_id, policy)`, `authenticate(secret, protocol)`, `rotate(key_id, version)`, `revoke(key_id, version)`, `update_policy(key_id, version, policy)` |
| 4 | `Ledger(db)`; async `reserve(Admission) -> Hold`, `mark_dispatched(request_id, attempt_id)`, `resize(Hold, Bounds)`, `settle(request_id, Usage)`, `release_unspent(request_id, evidence)`, `mark_pending(request_id, reason)`, `adjust(request_id, amount_micro, actor, reason)` |
| 5 | `Catalog(db, vault)`; async `create_provider(config)`, `put_credential(provider_id, secret)`, `put_model(model)`, `put_binding(binding)`, `candidates(principal, request) -> list[RouteSnapshot]` |
| 6 | `EgressPolicy.resolve_and_check(root) -> ResolvedTarget`; `Transport.open(route, proxy, request) -> async context manager[UpstreamResponse]` |
| 7 | `ProxyManager.acquire(selection, owner, deadline) -> async context manager[ProxyLease]`; async `release_kiot(key_id, actor)`; `ProxyLease.proxy` is old `ProxyConfig` or None |
| 8 | `WireCodec.decode(body, options) -> InferenceRequest`; `encode_result(result) -> dict`; `encode_event(event) -> list[bytes]`; instances keep state per request |
| 9–15 | `ProviderAdapter.open(request, route, lease) -> async context manager[ProviderStream]`; `ProviderStream.events` is `AsyncIterator[InferenceEvent]`; `ProviderStream.cancel()` is async |
| 10 | `Engine.prepare(principal, request, meta) -> PreparedCall`; async `PreparedCall.collect() -> InferenceResult`; `PreparedCall.events() -> AsyncIterator[InferenceEvent]`; async `PreparedCall.close()` |
| 11 | `ContinuationStore(db, vault)`; async `issue(scope, upstream_id, expires_at) -> str`; `resolve(handle, scope) -> ContinuationBinding` |
| 15 | `CredentialService(db, vault, proxies, transport)`; async `import_record(record, profile_id)`, `fresh_tokens(credential_id, deadline)` |
| 17 | `create_admin_router(services, authorized) -> APIRouter`; use existing admin session dependency, never client-key auth |

Record ownership: Task 3 defines `Principal(customer_id, key_id, policy_version)` and
`KeyPolicy`; Task 4 defines `Admission`, `Bounds`, `Hold`, `Usage`; Task 5 defines
`RouteSnapshot`; Task 8 defines request/event/result; Task 10 defines request meta;
Task 11 defines continuation scope/binding. Subsequent tasks import, not redefine.

---

### Task 1: Optional service database and an isolated PostgreSQL test harness

**Files:** create `gateway/settings.py`, `gateway/storage/{db.py,schema.py,migrations/env.py}`;
create `gateway/storage/migrations/versions/0001_identity.py`, `alembic-gateway.ini`,
`tests/gateway/conftest.py`, `tests/gateway/integration/test_database.py`,
`scripts/gateway_testdb.py`; modify `pyproject.toml`, `MANIFEST.in`, `.gitignore` and
`tests/conftest.py: deny_outbound_network` only for explicit test-server allowances.

**Interfaces:** consumes existing Settings without changing its defaults; produces
`Database`, async session factory, migrations and test-only `pg_db` fixture.

- [ ] Write a test proving migrations create `customers`, `api_keys`, `audit_events`
  and that old `autobuild_json.api` imports without installing service extras:

```python
import pytest
from sqlalchemy import text

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_identity_schema_has_unique_secret_digest(pg_db):
    async with pg_db.sessions() as session:
        rows = await session.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'api_keys'"
        ))
        assert any("UNIQUE" in row[0] and "secret_digest" in row[0] for row in rows)
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/integration/test_database.py -q`;
  expected RED from missing fixture/schema, not a silently skipped database test.
- [ ] Add service extras (`sqlalchemy>=2.0,<3`, `psycopg[binary,pool]>=3.2,<4`,
  `alembic>=1.14,<2`, `httpx[socks]>=0.28,<1`, `cryptography>=44`) and gateway-test
  extra (`pytest-asyncio>=0.25,<2`). Keep base dependencies independent of PostgreSQL.
  Add a `proxy` extra containing `httpx[socks]>=0.28,<1` for standalone Kiot use;
  importing legacy direct/static OAuth must not import optional service libraries.
  `ServiceSettings` requires database URL and master-key file only when enabled;
  secrets use `SecretStr`/hidden repr. Add resource packaging for migrations.
- [ ] Implement the async engine without credential logging:

```python
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

def build_sessions(url):
    engine = create_async_engine(
        url.get_secret_value(), pool_pre_ping=True, echo=False, hide_parameters=True
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)
```

- [ ] `gateway_testdb.py` accepts `--url-env AUTOBUILD_TEST_DATABASE_URL` or
  `--postgres-bin /absolute/path`, creates a random `abgw_test_<uuid>` database using
  a test-only role, and records only database ID/port in its owner-only temp manifest.
  The second mode uses `mkdtemp` and `initdb`/`pg_ctl` on loopback, no system service.
  Refuse production-looking names, missing dedicated-test acknowledgment, or broad
  cleanup paths. Cleanup closes connections and drops only the database it created.
  If binaries/URL are unavailable, return an actionable nonzero status; do not
  auto-install Docker, use SQLite, or claim database coverage.
- [ ] `pg_db` applies migrations to an isolated schema per test and resets only that
  schema after verifying its generated name. Permit socket connections exclusively
  to the explicit test database host/port; keep provider network blocked. Do not
  wrap each test in an outer rollback transaction, since concurrency tests need
  independent committed connections. Test the network guard against another port.
- [ ] GREEN: rerun the database test with a prepared test server, run legacy unit
  tests, then commit these exact paths as `feat: add optional gateway database`.

### Task 2: Secret vault and client-key primitives

**Files:** create `gateway/secrets.py`, `gateway/errors.py`,
`tests/gateway/unit/test_secrets.py`. No real keys in fixtures.

**Interfaces:** consumes Task 1 configuration; produces `Ciphertext(key_id, nonce,
ciphertext)`, `Vault`, `issue_key`, `key_digest`, `GatewayError(code, status, stage,
retry_after=None)`. Error messages are local safe constants, never provider text.

- [ ] RED test wrong-record AEAD binding and secret entropy/one-way digests:

```python
from uuid import UUID
import pytest
from autobuild_json.gateway.secrets import Vault, issue_key, key_digest

def test_secret_cannot_be_moved_between_records():
    vault = Vault({"v1": bytes(range(32))}, active="v1")
    a, b = UUID(int=1), UUID(int=2)
    encrypted = vault.seal("credential", a, b"synthetic-upstream-key")
    assert vault.open("credential", a, encrypted) == b"synthetic-upstream-key"
    with pytest.raises(ValueError):
        vault.open("credential", b, encrypted)
    prefix, secret = issue_key()
    assert secret.startswith(prefix)
    assert key_digest(secret, b"test-pepper") != secret.encode()
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/unit/test_secrets.py -q`; expect
  missing module/function RED, then implement AESGCM with a fresh 12-byte nonce:

```python
import secrets
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

def encrypt_record(key, kind, record_id, value):
    nonce = secrets.token_bytes(12)
    aad = f"abgw:v1:{kind}:{record_id}".encode()
    return nonce, AESGCM(key).encrypt(nonce, value, aad)
```

- [ ] Implement client secret generation using `token_urlsafe(32)` plus a nonsecret
  prefix; HMAC-SHA256 digest with domain separation. Reject malformed encoded keys,
  missing vault key versions and modified ciphertext. Repr/JSON serializers hide
  plaintext. Rotation retains old decryption keys until re-encryption completes.
- [ ] Test distinct nonces, wrong master key, bit flip, empty keyring, malformed
  key file permissions on POSIX, Unicode error safety, no secrets in exceptions.
- [ ] GREEN command above; commit `feat: add gateway secret vault and key hashing`.

### Task 3: Customer and API-key policy service

**Files:** create `gateway/identity/{service.py,policy.py}`;
create `tests/gateway/integration/test_identity.py` and
`tests/gateway/unit/test_key_policy.py`; extend migration 0001 before its first
release rather than editing a migration already applied to user data.

**Interfaces:** consumes Database and key primitives; produces Principal, KeyPolicy
and IdentityService signatures in the contract table. `create_customer` returns
UUID; `create_key`/`rotate` return frozen `IssuedKey(key_id: UUID, secret: str,
version: int)` only on that operation; secret has hidden repr.
KeyPolicy fields: model_ids, protocols, total/day/month limits in micro-units,
input/output overrides keyed by model, rpm, concurrency, expires_at, enabled.

- [ ] RED integration test rotation keeps key ID and rejects the old secret:

```python
import pytest
from autobuild_json.gateway.identity.service import IdentityService
from autobuild_json.gateway.identity.policy import KeyPolicy
from autobuild_json.gateway.errors import GatewayError

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_rotation_preserves_identity_but_revokes_secret(pg_db):
    service = IdentityService(pg_db, pepper=b"synthetic-pepper")
    owner = await service.create_customer("Test customer")
    issued = await service.create_key(owner, KeyPolicy(protocols={"openai"}))
    rotated = await service.rotate(issued.key_id, issued.version)
    assert rotated.key_id == issued.key_id
    assert (await service.authenticate(rotated.secret, "openai")).key_id == issued.key_id
    with pytest.raises(GatewayError, match="invalid_api_key"):
        await service.authenticate(issued.secret, "openai")
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/integration/test_identity.py -q`.
- [ ] Implement mutation + audit in one transaction, optimistic version updates
  and soft deletion. Lock customer and key in fixed order; authentication checks
  current enabled/expiry state using database time, constant-time digest comparison
  and allowed protocol. A default policy has zero quota, empty models, finite RPM
  and concurrency. Do not let authentication imply model permission.

```sql
UPDATE api_keys
SET secret_digest = :digest, version = version + 1
WHERE id = :key_id AND version = :expected_version AND revoked_at IS NULL
RETURNING id, version;
```

- [ ] Add tests for customer disable, expired key, empty allowlist, update conflict,
  revoke idempotence, protocol rejection, forbidden raw-secret retrieval and audit
  redaction. Assert no usage table is deleted on key deletion.
- [ ] GREEN focused tests plus `tests/test_security.py`; commit
  `feat: manage gateway customers and API key policies`.

### Task 4: Atomic quota reservations, settlement and upstream cost budgets

**Files:** create `gateway/metering/{units.py,ledger.py,costs.py,records.py}`,
`storage/migrations/versions/0002_metering.py`, `tests/gateway/unit/test_units.py`,
`tests/gateway/integration/{test_ledger.py,test_ledger_races.py}`.

**Interfaces:** consumes Principal/policy. Produces `Bounds(input_tokens,
output_tokens)`, `Usage(input_tokens, output_tokens, cached_read=0, cached_write=0,
reasoning=0, source="provider")`, `Admission(request_id, principal, model_id,
bounds, input_micro, output_micro, deadline, idempotency_digest=None)`, Hold and Ledger.
Hold identifies request/window snapshot; it does not contain input messages.

- [ ] Write arithmetic test and run `.venv/bin/python -m pytest
  tests/gateway/unit/test_units.py -q` to get RED:

```python
from autobuild_json.gateway.metering.units import coefficient_micro, weighted_micro

def test_weighted_usage_is_exact_and_not_currency():
    assert weighted_micro(1000, 500, coefficient_micro("1"),
                          coefficient_micro("3")) == 2_500_000_000
    assert coefficient_micro("0.000001") == 1
```

- [ ] Implement Decimal parsing of string inputs, exact integer scaling, reject
  non-finite/negative/overprecision values and values outside NUMERIC(38,0). Zero
  coefficients remain valid. Store token counts as nonnegative BIGINT and quota
  micro-units as NUMERIC(38,0). HTTP payload must not parse coefficients through float.

```python
from decimal import Decimal, localcontext

def coefficient_micro(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid_coefficient")
    number = Decimal(value)
    if not number.is_finite() or number < 0 or number >= Decimal("1e32"):
        raise ValueError("invalid_coefficient")
    with localcontext() as context:
        context.prec = 80
        scaled = number * 1_000_000
        if scaled != scaled.to_integral_value():
            raise ValueError("invalid_coefficient")
    return int(scaled)

def weighted_micro(input_tokens, output_tokens, input_micro, output_micro):
    values = (input_tokens, output_tokens, input_micro, output_micro)
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("invalid_usage")
    total = input_tokens * input_micro + output_tokens * output_micro
    if total >= 10 ** 38:
        raise ValueError("quota_overflow")
    return total
```

- [ ] Migration adds requests, attempts, quota buckets, reservations, immutable
  ledger entries, cost schedules/budgets, adjustments and active slots. Unique
  `(request_id, entry_kind)` prevents duplicate settlement; unique key/idempotency
  digest prevents replay. Use explicit state enum/checks, not arbitrary strings.
- [ ] Integration fixture `metering_case` (owned in test_ledger.py) creates a test
  customer/key via Task 3 with total 1,000 micro-units, model `metering-test` allowed
  and coefficient integers `input_micro=1, output_micro=1` (one micro-unit per raw
  token). `.admission(amount)` creates a new request UUID, input bound `amount`,
  output bound 0 and finite deadline. `.ledger`, `.admission(amount)`, `.balance()` are its
  only helpers, with `.balance()` returning a dataclass with `spent`, `held`,
  `available` integer fields read from DB.
  Test two independent sessions racing for the last 700 units:

```python
import asyncio
import pytest
from autobuild_json.gateway.errors import GatewayError

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_racing_reservations_cannot_overspend(metering_case):
    c = metering_case
    results = await asyncio.gather(
        c.ledger.reserve(c.admission(700)),
        c.ledger.reserve(c.admission(700)), return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert sum(isinstance(r, GatewayError) for r in results) == 1
    assert (await c.balance()).available == 300
```

- [ ] `reserve` locks customer/key then ordered quota buckets, validates snapshot,
  atomically increments held/RPM/slot and writes request. Never hold transaction
  over network. `settle` locks request/buckets, requires nonterminal reservation,
  subtracts hold and adds spent once; duplicate calls return existing settlement.
  `release_unspent` needs explicit pre-dispatch/rejection evidence. `mark_pending`
  keeps held; `adjust` records admin actor/reason and never mutates raw usage.
- [ ] Add failing tests then implement UTC rollover, coefficient edit mid-request,
  null vs zero quota, pending >24h, missing usage, death after dispatch, reservation
  above actual, provider usage above bound, DB failure, duplicate idempotency claim
  and payload mismatch. Run concurrency tests in separate processes too, not only
  coroutines sharing one session. Assertions query committed DB state.
- [ ] Add independent upstream cost estimation/budget holds; missing price/usage
  means null cost. Use Decimal currency values; do not sum currencies. Retry attempt
  cost is service cost; only the serving attempt affects customer quota. Test cached
  input bucket splitting and route-switch reservation increases before dispatch.
- [ ] GREEN both unit/integration commands; commit
  `feat: account for gateway quota with atomic reservations`.

### Task 5: Provider/model registry and deterministic routing

**Files:** create `gateway/providers/catalog.py`, `gateway/routing/{select.py,records.py}`,
`storage/migrations/versions/0003_catalog.py`, `tests/gateway/unit/test_routing.py`,
`tests/gateway/integration/test_catalog.py`.

**Interfaces:** consumes Vault/Principal/Ledger; produces Catalog and immutable
RouteSnapshot with provider_id, credential_id, public_model_id, upstream_model,
adapter, root, config_version, capability set, bounds, proxy selection, priority.
Secrets are opened only at dispatch, never included in route repr/client output.
References to canonical request types defined in Task 8 use TYPE_CHECKING and
postponed annotations until that task exists; pure routing tests use field-based
inputs and must not require importing a not-yet-created contracts module.

- [ ] RED test before writing the selector:

```python
from autobuild_json.gateway.routing.select import eligible

def test_alias_never_grants_a_hidden_model():
    assert not eligible(
        resolved_model="private-model", allowed_models={"public-model"},
        required={"tools"}, supported={"text", "tools"}, healthy=True,
    )
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/unit/test_routing.py -q`.
  Implement the gate without making aliases their own authorization principal:

```python
def eligible(resolved_model, allowed_models, required, supported, healthy):
    return (healthy and resolved_model in allowed_models
            and required <= supported)
```

- [ ] Add schema for providers, encrypted credentials, public models, aliases,
  bindings, immutable config versions and cooldowns. Catalog methods validate
  adapter type, finite bounds, enabled status, public IDs and version conflicts.
  Store auth mode explicitly, never infer from key prefixes.
- [ ] Implement priority then round-robin using DB-owned cursor under lock; freeze
  route snapshots. Native model discovery saves disabled candidates for review.
  Same-model fallback checks identity, capabilities and cooldown; custom alias
  spanning different models must be marked as a router model in admin metadata.
- [ ] Test no model leaks in discovery, revoked credential, alias cycles, provider
  edit while in flight, model removed while in flight, disabled all routes, priority
  fairness and provider-wide vs credential-scoped 429. Return safe distinct errors.
- [ ] GREEN unit/integration suites; commit `feat: configure upstream providers and model routing`.

### Task 6: Secure egress and bounded HTTP transport

**Files:** create `gateway/transport/{egress.py,http.py,network.py}`,
`tests/gateway/unit/{test_egress.py,test_transport.py}`.

**Interfaces:** consumes RouteSnapshot and existing ProxyConfig; produces
EgressPolicy, ResolvedTarget(host, port, approved_addresses, tls_name), Transport,
UpstreamResponse(status, headers, byte iterator, close/cancel). Header/body size
and deadlines are enforced while reading, not after buffering everything.

- [ ] RED test for a DNS answer containing metadata IP, including mapped IPv6:

```python
import pytest
from autobuild_json.gateway.transport.egress import validate_addresses

@pytest.mark.parametrize("addresses", [
    ["93.184.216.34", "169.254.169.254"], ["::ffff:127.0.0.1"], ["::1"],
])
def test_private_dns_answer_is_rejected(addresses):
    with pytest.raises(ValueError, match="egress_denied"):
        validate_addresses(addresses, allowed_networks=())
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/unit/test_egress.py -q`.
  Implement IP classification, URL normalization, no userinfo/query/fragment root,
  allowlisted private origins/CIDRs, TLS verification and no redirects by default.

```python
import ipaddress

def validate_addresses(addresses, allowed_networks):
    if not addresses:
        raise ValueError("egress_denied")
    networks = [ipaddress.ip_network(n) for n in allowed_networks]
    for raw in addresses:
        ip = ipaddress.ip_address(raw)
        ip = getattr(ip, "ipv4_mapped", None) or ip
        allowed = any(ip.version == n.version and ip in n for n in networks)
        if not ip.is_global and not allowed:
            raise ValueError("egress_denied")
```

- [ ] At connect time, resolve and validate again through an injected httpcore
  network backend, pin a validated IP while retaining original TLS SNI/Host. Test
  resolver changes between configuration and connect. HTTPX uses `trust_env=False`,
  finite pool limits and streaming reads. No unrestricted retry middleware.
- [ ] Proxy transport validates the proxy endpoint too. For proxy-side DNS, require
  trusted egress policy configured by operator; expose its assurance separately
  from direct DNS pinning. Test no direct connection after proxy failure.
- [ ] Forbid forwarding caller auth, cookies, Host or hop-by-hop headers. Tests
  assert safe headers only, redirects cannot move credentials, TLS remains on,
  environment proxy is ignored, connect/idle/total deadlines cancel reads, error
  messages omit secret URLs and raw upstream bodies.
- [ ] GREEN transport/unit tests and legacy `tests/test_transport.py`; commit
  `feat: enforce bounded gateway egress and HTTP transport`.

### Task 7: Proxy profiles, shared leases and KiotProxy lifecycle

**Files:** create `gateway/proxy/{config.py,leases.py,kiot.py,manager.py}`,
`storage/migrations/versions/0004_proxy_leases.py`,
`tests/gateway/unit/{test_proxy_profiles.py,test_kiot.py}`,
`tests/gateway/integration/test_proxy_leases.py`.

**Interfaces:** consumes old parse_proxies, Vault, Database and Transport primitives;
produces ProxySelection(mode, profile_id, runtime_entries), ProxyLease(proxy, owner,
generation, deadline), ProxyManager. Lease storage implementations expose identical
`claim`, `renew`, `release`, `assert_owner` semantics; RAM only for standalone OAuth.

- [ ] RED pure lifecycle test; `lease_action` takes conservative seconds remaining:

```python
from autobuild_json.gateway.proxy.kiot import lease_action

def test_cooldown_never_interrupts_active_operation():
    assert lease_action(active=True, ttl=1200, cooldown=0,
                        required=180, rotate=True) == "busy"
    assert lease_action(active=False, ttl=500, cooldown=59,
                        required=180, rotate=False) == "reuse"
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/unit/test_kiot.py -q`.
  Implement the decision kernel and then API I/O separately:

```python
def lease_action(active, ttl, cooldown, required, rotate):
    if active:
        return "busy"
    if ttl >= required + 15 and (not rotate or cooldown > 0):
        return "reuse"
    if cooldown > 0:
        return "wait"
    return "new"
```

- [ ] Validate/dedup each nonblank Kiot key line without echoing it. HTTP/SOCKS5 and
  region enums are explicit. Implement current/new/out with a dedicated fixed-root
  client, 10-second cancellable acquisition, response byte limit, sanitized provider
  codes, no raw messages or query logging. Reconcile `/new` timeout via current.
  Keep RAM lease/config/Kiot modules independent of SQLAlchemy; lazily import the
  PG lease backend only in service mode. The optional proxy extra suffices for
  standalone Kiot operation, with an actionable error when it is not installed.
- [ ] Parse epoch-millisecond expiry and nextRequestAt using provider timestamp
  plus local monotonic observations; choose conservative TTL/cooldown when hints
  disagree. Reject nonfinite/negative times, malformed endpoint and missing data.
- [ ] Use DB row claims keyed by secret fingerprint, endpoint capacity and fencing
  generation. One Kiot operation/key; reclaim only after old hard deadline +15s.
  Renew failure cancels owner operation. Snapshot resolved proxy through request.
  `/out` is explicit admin action and only allowed when idle.
- [ ] Tests: missing current, wrong key, error body containing full key, cooldown
  reuse vs wait, expiration before deadline, cancel during wait, ambiguous allocation,
  duplicate keys/endpoints, fixed proxy auth encoding, concurrent OAuth/inference
  owners, stale release/renew, crash grace period and never direct fallback.
- [ ] GREEN unit + PostgreSQL lease races; commit `feat: add shared proxy pools and KiotProxy leases`.

### Task 8: Canonical requests/events and robust streaming parsers

**Files:** create `gateway/contracts.py`, `gateway/protocols/{common.py,frames.py}`,
`tests/gateway/unit/{test_contracts.py,test_frames.py}`.

**Interfaces:** produces Pydantic discriminated content blocks Text, Image, ToolCall,
ToolResult, Opaque; InferenceRequest(model, messages, instructions, tools, options,
stream, continuation); InferenceResult(id, model, blocks, finish_reason, usage).
InferenceEvent has kind, item_id, index, delta, block, finish_reason and usage;
kind enum: started/block_started/text_delta/tool_delta/block_finished/usage/finished/error.
Provider-specific opaque data is scoped to adapter and never translated blindly.

- [ ] RED framing test:

```python
from autobuild_json.gateway.protocols.frames import SSEDecoder

def test_sse_utf8_split_and_multiline_data():
    decoder = SSEDecoder(max_frame_bytes=1024)
    wire = 'event: text\ndata: {"text":"chào"}\n\n'.encode()
    events = []
    for byte in wire:
        events.extend(decoder.feed(bytes([byte])))
    assert events == [("text", '{"text":"chào"}')]
    assert decoder.finish() == []
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/unit/test_frames.py -q`.
  Implement byte-buffer boundary parsing (including CRLF and multiline data),
  incremental UTF-8 decoding only after full frame boundary, and NDJSON equivalent.
  Oversize/truncated frames raise safe protocol errors, not partial success.

```python
def join_sse_data(lines):
    values = []
    for line in lines:
        if line.startswith("data:"):
            value = line[5:]
            values.append(value[1:] if value.startswith(" ") else value)
    return "\n".join(values)
```

- [ ] Add event-state validator: stable item/call IDs, exactly one terminal state,
  bounded 128 calls/1 MiB arguments, interleaved calls keyed by ID/index; never parse
  incomplete tool JSON as finished input. Empty legitimate output is not an error.
- [ ] Test malformed JSON, split CRLF, BOM, comments/heartbeats, multiple frames per
  read, truncated UTF-8, terminal error without done, duplicate terminal and signed
  blocks from a different provider. Model validation forbids unknown semantic options.
- [ ] GREEN unit tests; commit `feat: define typed gateway events and stream framing`.

### Task 9: OpenAI Chat Completions codec and outbound adapter

**Files:** create `gateway/protocols/openai_chat.py`, `gateway/providers/openai.py`,
`tests/gateway/unit/test_openai_chat.py`, synthetic OpenAI fixtures.

**Interfaces:** consumes canonical records, Transport and RouteSnapshot; produces
OpenAIChatCodec and OpenAIAdapter implementing shared contracts. Pure helper
`normalize_openai_usage(payload) -> Usage` uses inclusive totals.

- [ ] RED test cache/reasoning double-count prevention:

```python
from autobuild_json.gateway.providers.openai import normalize_openai_usage

def test_openai_usage_subtotals_are_not_added_again():
    usage = normalize_openai_usage({
        "prompt_tokens": 100, "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 80},
        "completion_tokens_details": {"reasoning_tokens": 20},
    })
    assert (usage.input_tokens, usage.output_tokens) == (100, 50)
    assert (usage.cached_read, usage.reasoning) == (80, 20)
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/unit/test_openai_chat.py -q`.
  Implement message/system/tool mapping and usage normalizer:

```python
from autobuild_json.gateway.metering.records import Usage

def normalize_openai_usage(payload):
    return Usage(
        input_tokens=payload["prompt_tokens"],
        output_tokens=payload["completion_tokens"],
        cached_read=payload.get("prompt_tokens_details", {}).get("cached_tokens", 0),
        reasoning=payload.get("completion_tokens_details", {}).get("reasoning_tokens", 0),
    )
```

- [ ] Parse streaming `choices[].delta`, argument fragments and usage-only chunks;
  encode role delta, stable IDs, finish reason and `[DONE]` only after success.
  Require n=1, text/tool core; reject unsupported functions/options explicitly.
  Request stream usage from supported upstreams, but absent usage stays unknown.
- [ ] Test tool-call/result round trip, stop/length mapping, upstream auth headers,
  fake HTTP 401/429/5xx, usage before/after finish, empty choices, client stream=false
  collection and native upstream stream=true. Do not embed retries in the adapter.
- [ ] GREEN focused tests; commit `feat: implement OpenAI chat protocol adapter`.

### Task 10: Public gateway and the first end-to-end metered route

**Files:** create `gateway/http/{app.py,auth.py,boundary.py,streaming.py}`, `gateway/engine.py`,
`tests/gateway/integration/{test_gateway.py,test_stream_lifecycle.py}`,
`tests/gateway/harness.py` and `tests/gateway/fixtures/openai_success.json`.

**Interfaces:** consumes Tasks 1–9; produces Engine, PreparedCall, `create_gateway_app`
and RequestMeta(request_id, admitted_at, deadline, idempotency_digest). The harness
builds real services against pg_db, injects MockTransport only at provider I/O,
and seeds one customer/key/model/route with finite bounds. It returns `.client`,
`.secret`, `.ledger`, `.request_id`, `.upstream_requests`; client is HTTPX ASGI client.

- [ ] RED integration test client traffic without Origin and no admin surface:

```python
import pytest

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_public_gateway_has_no_admin_routes(gateway_env):
    env = gateway_env
    response = await env.client.post("/v1/chat/completions", json={
        "model": "test-model", "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 8,
    }, headers={"Authorization": "Bearer " + env.secret})
    assert response.status_code == 200
    assert response.json()["usage"]["prompt_tokens"] == 4
    assert (await env.client.get("/api/jobs")).status_code == 404
    assert env.secret not in str(env.upstream_requests)
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/integration/test_gateway.py -q`.
- [ ] Implement shared lifecycle in Engine: authenticate current policy → decode/
  resolve capabilities → candidate route snapshot/bounds → reserve → acquire proxy/
  credential → mark dispatched → open upstream → stream/collect → settle/release/
  pending → finally close/release. Use context managers; no outbound calls inside
  a DB transaction. Guard each transition with durable state/fencing checks.

```python
async def finalize_usage(ledger, request_id, usage, dispatched):
    if usage is not None:
        await ledger.settle(request_id, usage)
    elif dispatched:
        await ledger.mark_pending(request_id, "usage_missing")
    else:
        await ledger.release_unspent(request_id, "not_dispatched")
```

- [ ] ASGI stream response waits for upstream open before response.start; after
  commit errors are encoded by the selected codec. Race receive-disconnect against
  send/read; cancel upstream and execute cleanup under a bounded shield so repeated
  cancellation cannot leak leases. Buffer at most one bounded event/frame ahead.
  Persist final accounting before encoding the success terminal event; when final
  usage is absent, use the documented pending/error outcome rather than making a
  verified-success claim. Do not flush a success terminal then discover commit failed.
- [ ] Gateway auth allows protocol headers, rejects conflicting duplicates and URL
  credentials, applies Host/body/origin policy independent of old admin middleware.
  Add unauthenticated request rate/body limits without leaking key existence.
- [ ] Tests: final usage dropped, duplicate chunks, lost DB during settlement,
  disconnect before headers vs midstream, proxy acquisition failure, one allowed
  precommit retry, ambiguous POST timeout not retried, refused model, quota exceeded,
  no prompt in logs and no HTTP200 on upstream-open failure. Assert held/spent/slots
  and exact outbound attempt count for each case.
- [ ] GREEN integration suite plus legacy tests; commit
  `feat: serve a metered OpenAI-compatible gateway`.

### Task 11: Responses API and tenant-scoped continuations

**Files:** create `gateway/protocols/openai_responses.py`,
`gateway/routing/continuations.py`, `storage/migrations/versions/0005_continuations.py`,
`tests/gateway/unit/test_responses.py`, `tests/gateway/integration/test_continuations.py`;
extend OpenAIAdapter and public routes.

**Interfaces:** ContinuationScope(customer_id, key_id, public_model_id),
ContinuationBinding(route_id, credential_id, encrypted_upstream_id, expires_at).
ResponsesCodec shares the event model; adapter selects the native Responses suffix.

- [ ] RED validation test before implementing unsupported-option handling:

```python
import pytest
from autobuild_json.gateway.protocols.openai_responses import ResponsesCodec
from autobuild_json.gateway.errors import GatewayError

def test_responses_does_not_silently_enable_background_storage():
    with pytest.raises(GatewayError, match="unsupported_feature"):
        ResponsesCodec().decode(
            {"model": "m", "input": "hi", "background": True}, options={}
        )
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/unit/test_responses.py -q`.
  Implement input/content/tool mapping, output item IDs, function_call arguments,
  call outputs and ordered semantic SSE events. Never use Chat chunks on this route.

```python
def continuation_lookup_sql():
    return (
        "SELECT * FROM continuation_handles WHERE handle_digest = :digest "
        "AND customer_id = :customer AND key_id = :key AND public_model_id = :model "
        "AND expires_at > CURRENT_TIMESTAMP"
    )
```

- [ ] Native continuation maps both response handle and opaque item references;
  no cross-key/provider fallback. store=true/background/compact/retrieval are
  unsupported; continuation only on routes verified to support it without those
  features. Otherwise reject previous_response_id with a clear client error.
- [ ] Test cross-key handle, expired handle, route disabled, same customer/different
  key, reasoning opaque blocks, usage subtotals, tool-call IDs and terminal failure.
  No persistence of prompts; DB handle blob must be encrypted.
- [ ] GREEN Responses + continuation integration; commit
  `feat: support Responses events and scoped continuation handles`.

### Task 12: Anthropic Messages inbound/outbound compatibility

**Files:** create `gateway/protocols/anthropic.py`, `gateway/providers/anthropic.py`,
`tests/gateway/unit/test_anthropic.py`, `tests/gateway/integration/test_anthropic_gateway.py`;
extend routes and adapter registry.

**Interfaces:** AnthropicCodec/AnthropicAdapter use the shared signatures;
`normalize_anthropic_usage(payload) -> Usage` adds exclusive input cache buckets.

- [ ] RED cache accounting test:

```python
from autobuild_json.gateway.providers.anthropic import normalize_anthropic_usage

def test_anthropic_cache_buckets_are_added_once():
    u = normalize_anthropic_usage({"input_tokens": 10, "output_tokens": 20,
        "cache_read_input_tokens": 70, "cache_creation_input_tokens": 30})
    assert (u.input_tokens, u.output_tokens) == (110, 20)
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/unit/test_anthropic.py -q`.
  Fetch official Messages/version/stream docs at execution, record citations and
  pin version in fixtures before implementing payload mappings. No code copying.

```python
def anthropic_total_input(payload):
    return (payload["input_tokens"] + payload.get("cache_read_input_tokens", 0)
            + payload.get("cache_creation_input_tokens", 0))
```

- [ ] Implement system/content blocks, tool_use/tool_result, stop_reason, version
  header validation, native x-api-key/Bearer inbound and Messages SSE lifecycle.
  count_tokens uses a real matching counter or unsupported; model list uses the
  Anthropic schema only for its versioned request, filtered by current policy.
- [ ] Test Anthropic client → OpenAI route and native Anthropic route, tool fragments
  interleaved with text, cache-control not dropped, unsupported beta header, signed
  block rejected cross-provider, final usage retained and one shared quota.
- [ ] GREEN unit + integration; commit `feat: translate Anthropic Messages requests and streams`.

### Task 13: Gemini native inbound/outbound compatibility

**Files:** create `gateway/protocols/gemini.py`, `gateway/providers/gemini.py`,
`tests/gateway/unit/test_gemini.py`, `tests/gateway/integration/test_gemini_gateway.py`.

**Interfaces:** GeminiCodec/GeminiAdapter; model detail/list/count return filtered
native shapes. `gemini_output_total(payload) -> int` includes thoughts exactly once
for the verified native usageMetadata schema.

- [ ] RED test query credentials vs allowed stream query:

```python
import pytest
from autobuild_json.gateway.protocols.gemini import validate_query

def test_gemini_query_allows_sse_but_not_secret():
    assert validate_query({"alt": "sse"}, streaming=True) == "sse"
    with pytest.raises(ValueError, match="query_credentials_forbidden"):
        validate_query({"key": "synthetic"}, streaming=True)
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/unit/test_gemini.py -q`.
  Read official GenerateContent/stream/count docs, record usage-field inclusivity,
  then implement contents/systemInstruction/function declarations/calls/responses,
  generationConfig and safe model suffix escaping.

```python
def validate_query(query, streaming):
    if "key" in query:
        raise ValueError("query_credentials_forbidden")
    if streaming and query == {"alt": "sse"}:
        return "sse"
    if not streaming and not query:
        return "json"
    raise ValueError("unsupported_query")
```

- [ ] Native usage normalizer verifies candidates/thought/cache totals against
  fixtures instead of guessing from totalTokenCount. Expose explicit source/unknown
  when fields do not suffice. No thoughts/signatures forged on translated routes.
- [ ] Test Gemini→OpenAI core, native Gemini, unknown action, model path injection,
  function result round-trip, SSE fragmentation, auth conflict, stop reasons and
  count failure without quota generation charge.
- [ ] GREEN unit/integration; commit `feat: support Gemini native API protocol`.

### Task 14: Authenticated Ollama emulation and outbound adapter

**Files:** create `gateway/protocols/ollama.py`, `gateway/providers/ollama.py`,
`tests/gateway/unit/test_ollama.py`, `tests/gateway/integration/test_ollama_gateway.py`.

**Interfaces:** OllamaCodec/OllamaAdapter; no unauthenticated local exception on the
public gateway. Expose only chat/generate/tags/show/version from the spec.

- [ ] RED finite record encoding test:

```python
import json
from autobuild_json.gateway.protocols.ollama import ndjson_record

def test_ollama_ndjson_is_one_complete_unicode_record():
    wire = ndjson_record({"message": {"content": "chào"}, "done": False})
    assert wire.endswith(b"\n") and wire.count(b"\n") == 1
    assert json.loads(wire)["message"]["content"] == "chào"
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/unit/test_ollama.py -q`.
  Read official Ollama API contracts, map stream default true, options num_predict,
  messages/tools and generate prompt/system; reject context-array continuation,
  unsupported load/model-management commands and options not representable.

```python
import json

def ndjson_record(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
```

- [ ] Final done record contains real prompt_eval_count/eval_count when known;
  unknown usage does not become zero. Tests cover native tools, JSON collection,
  malformed frame, no bearer, model filtering and no collision with private `/api`.
- [ ] GREEN unit/integration; commit `feat: provide authenticated Ollama-compatible endpoints`.

### Task 15: OAuth import, single-flight refresh and Codex outbound adapter

**Files:** create `gateway/accounts/{imports.py,refresh.py}`, `gateway/providers/codex.py`,
`storage/migrations/versions/0006_oauth.py`, `tests/gateway/unit/test_codex_adapter.py`,
`tests/gateway/integration/{test_oauth_import.py,test_refresh_races.py}`.

**Interfaces:** consumes existing SuccessRecord/identity verification and proxy leases;
produces CredentialService, encrypted token generation, CodexAdapter. `refresh_state`
maps only known structured error codes; arbitrary provider strings stay private.

- [ ] RED test ambiguous refresh never becomes an ordinary retry:

```python
from autobuild_json.gateway.accounts.refresh import refresh_state

def test_unknown_refresh_result_requires_reconciliation():
    assert refresh_state("timeout_after_send") == "refresh_uncertain"
    assert refresh_state("invalid_grant") == "reauth_required"
    assert refresh_state("success") == "active"
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/unit/test_codex_adapter.py -q`.
  Add imports with strict schema and issuer/account/workspace dedup, explicit admin
  selection, no scanning data directories. Validate fresh signed ID tokens when
  possible; stale imports remain unverified until authorized refresh/probe proves
  identity. Do not reject all expired ID tokens as irrecoverable credentials.

```sql
UPDATE credentials
SET encrypted_secret = :cipher, token_generation = token_generation + 1,
    health = 'active'
WHERE id = :id AND token_generation = :expected_generation
  AND refresh_owner = :owner
RETURNING token_generation;
```

- [ ] Refresh lease is distributed, includes ownership/generation; inspect latest
  committed token after waiting, never perform second refresh unnecessarily.
  Crash/timeout after grant send marks uncertain, no automatic reuse of old grant.
  Provider token expiry/401 differs from forbidden account/verification state.
- [ ] Implement Codex inference against the actual current HTTP contract established
  from authorized fixtures/live smoke. Add contract metadata for account header,
  accepted options, usage and model bounds; unsupported max output is rejected, not
  stripped. Do not treat a successful `/oauth/token` call as successful inference.
- [ ] Tests: two workers one refresh, stale writer denied, token chain swap atomic,
  missing refresh token, import duplicates, workspace mismatch, proxy preserved,
  export shape unchanged and no CheckLive dependency imported by gateway modules.
- [ ] GREEN offline/integration first. Live route stays unverified without a scoped
  operator-approved credential test; commit `feat: manage Codex OAuth credentials for gateway routes`.

### Task 16: Connect proxy selection to the existing OAuth workbench

**Files:** create `gateway/proxy/legacy.py`, `tests/gateway/unit/test_legacy_proxy_bridge.py`;
modify `autobuild_json/api_models.py: JobInput/LinkInput`, `api.py: start/link`,
`runner.py: start/_run/_execute`, `tests/test_api.py`, `tests/test_runner.py`.

**Interfaces:** old static `Runner.start(report, proxies, mode, workers, timeout)`
remains valid. Add optional keyword `proxy_source=None`; bridge holds a lease around
the entire processor call and adapts cancellation into async acquisition without
creating a new event loop per network request. Service mode uses the common PG
manager; standalone mode uses a single coordinator's RAM lease backend.

- [ ] RED backwards-compatible input test:

```python
from autobuild_json.api_models import JobInput

def test_static_proxy_payload_remains_accepted():
    item = JobInput(accounts_text="a@example.com|p|JBSWY3DPEHPK3PXP",
                    proxies_text="http://proxy.invalid:8080")
    assert item.proxy_mode == "legacy"
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/unit/test_legacy_proxy_bridge.py -q`.
  Add fields `proxy_mode` (legacy/direct/fixed/pool/kiotproxy/profile), secret Kiot
  lines, region/protocol and optional profile ID. Legacy default retains old parser;
  conflicting modes/secret fields return sanitized validation errors, never echo rows.

```python
def proxy_capacity(mode, entries, configured_workers):
    if mode == "direct":
        return configured_workers
    return min(configured_workers, len(entries))
```

- [ ] Reserve by source/key identity before submit; acquire actual lease before
  starting OAuth deadline, maintain durable progress stage `proxy_acquire`, cancel
  waits on Stop. Do not release proxy reservation before terminal write/cleanup.
  Retry attempts inside one OAuth operation retain proxy and original hard deadline.
- [ ] Manual callback flow receives a dedicated lease policy: no live proxy changes
  while token exchange runs; show browser proxy responsibility as before. Dynamic
  Kiot browser-session pinning is not promised; preserve explicit manual limitation.
- [ ] Tests include duplicate email, fewer keys than workers, stop during cooldown,
  storage failure, no fallback, source dedup and concurrent gateway sharing same key.
- [ ] GREEN legacy API/runner/browser tests; commit
  `feat: use proxy profiles and KiotProxy in the OAuth workbench`.

### Task 17: Private administration APIs and integrated Vietnamese UI

**Files:** create `gateway/admin/{routes.py,schemas.py,usage.py}`,
`gateway/admin/static/{index.html,app.js,api.js,keys.js,providers.js,models.js,proxies.js,usage.js,style.css}`,
`tests/gateway/integration/test_admin.py`, `tests/ui/gateway-state.test.mjs`,
`tests/ui/gateway-browser-smoke.mjs`; modify existing `api.py` through an optional
service mounting function, `static/index.html` navigation, packaging and npm scripts.

**Interfaces:** create_admin_router consumes service objects + existing authorized
dependency. Mount under private `/api/service/`; pages under `/service/` on admin
listener only. UI module `maskedSecret(label)` and shared fetch wrapper never reveal
stored upstream/client secrets. Service disabled leaves old screen operational.

- [ ] Before UI code, use the Superdesign skill with saved project/design direction;
  follow its resume/init checks and obtain the required UI draft approval. Upload
  only synthetic view data, no local credentials/source payload containing secrets.
  Continue backend API tests while waiting, but do not invent an approved canvas.
- [ ] RED admin permission test and a Node secret-rendering test:

```javascript
import test from 'node:test';
import assert from 'node:assert/strict';
import { maskedSecret } from '../../autobuild_json/gateway/admin/static/api.js';

test('stored key rendering is a label, never a secret', () => {
  assert.equal(maskedSecret('key-01'), 'key-01 ••••••••');
});
```

- [ ] Run `node --test tests/ui/gateway-state.test.mjs` and
  `.venv/bin/python -m pytest tests/gateway/integration/test_admin.py -q` (RED).
  Implement CRUD routes over services, never raw arbitrary SQL tables. Validate
  expected_version, typed IDs and secret write-only inputs. Use audit transaction.

```javascript
export function maskedSecret(label) {
  return `${label} ••••••••`;
}
```

- [ ] Implement customers/keys (issue once, rotate/revoke, model/protocol scope,
  coefficients and quota), provider/credential/mapping/pricing, model catalog,
  proxy modes/region/cooldown/release, OAuth import and usage/pending-adjustment
  screens. Use textContent, no string-built HTML with user labels. Secrets clear
  on success/logout; never localStorage/sessionStorage.
- [ ] Playground issues request only through a selected test client key held in
  memory; a private admin proxy invokes the same Engine with that key's Principal,
  accounts usage under it and verifies CSRF. Do not send admin cookie/key to public
  upstream. Bound streaming UI output and show raw vs weighted usage separately.
- [ ] Browser harness seeds synthetic PostgreSQL-backed services and mocked upstream,
  isolated ports/data. Test real CRUD, one-time secret display, XSS labels, stale
  response after logout, finite forms, redacted errors, mobile layout, model preview,
  Kiot wait/cancel and old workbench flow. Screenshots are local ignored artifacts.
- [ ] GREEN Node/browser + API/security suites; commit
  `feat: manage gateway service from the private dashboard`.

### Task 18: Recovery, operational limits, backups and opt-in deployment

**Files:** create `gateway/maintenance.py`, `gateway/storage/backup.py`,
`gateway/__main__.py`, `tests/gateway/integration/{test_recovery.py,test_backups.py}`,
`tests/gateway/unit/test_cli.py`, `docs/gateway-operations.md`,
`deploy/gateway-compose.yml`, `deploy/gateway.env.example`; modify README/package data.

**Interfaces:** CLI `python -m autobuild_json.gateway` starts only gateway mode;
private admin is still separately launched. `recover_request(state, dispatched)`
is a pure policy helper; maintenance leader applies transitions with fencing.

- [ ] RED recovery test:

```python
from autobuild_json.gateway.maintenance import recover_request

def test_crash_after_dispatch_keeps_usage_hold():
    assert recover_request("dispatched", dispatched=True) == "usage_pending"
    assert recover_request("reserved", dispatched=False) == "release_unspent"
```

- [ ] Run `.venv/bin/python -m pytest tests/gateway/integration/test_recovery.py
  tests/gateway/unit/test_cli.py -q`; implement leader lease, reconciliation and
  90-day metadata retention without deleting quota ledger/audit.
  Purge/redact only nonessential request metadata; retain IDs and foreign-key
  records referenced by immutable accounting. Test old ledger reports after cleanup.

```python
def recover_request(state, dispatched):
    if state in {"completed", "failed", "partial", "released"}:
        return "unchanged"
    return "usage_pending" if dispatched else "release_unspent"
```

- [ ] Backup exports a transaction-consistent database snapshot, encrypts the backup
  envelope, omits master-key material and restricts file permissions. Restore only
  into an explicit empty target, validate required key versions first; never overwrite
  production implicitly. Test rotation retaining old ciphertext decryptability and
  restore with missing key failing closed using synthetic records.
- [ ] Gateway CLI validates finite timeouts, host allowlist and TLS proxy settings;
  bind loopback by default, no access log secret queries. Health exposes only basic
  readiness. Graceful shutdown stops admission, bounds drain by operation deadlines,
  leaves uncertain holds pending and does not call Kiot `/out`.
- [ ] Compose is a template only: PostgreSQL internal network, gateway behind TLS
  reverse proxy, private admin not public, master-key file external to image/DB,
  migration command separate. Do not actually publish ports/domains at this task.
- [ ] Test two maintenance leaders, stale owner, midnight periods, shutdown in stream,
  quota DB outage, lease loss, secret rotation and old OAuth-only launch without
  service extras. Document pending adjustment, backup, compatibility and provider
  resale responsibilities without claiming subscription resale permission.
- [ ] GREEN focused suites; commit `feat: operate and recover the gateway safely`.

### Task 19: SDK compatibility matrix, end-to-end verification and handoff

**Files:** create `tests/gateway/sdk/{test_openai_sdk.py,test_anthropic_sdk.py,test_gemini_sdk.py,test_ollama_sdk.py}`,
`tests/gateway/integration/test_cross_protocol.py`, `requirements/gateway-sdk-tests.lock`,
`docs/gateway-compatibility.md`, `.github/workflows/gateway-tests.yml`;
update README/THIRD_PARTY and `docs/implementation-status.md` with actual evidence.

**Interfaces:** SDK tests consume the real gateway over a temporary loopback server,
not a hand-built SDK response; only outbound provider network is mocked. Reuse the
Task 10 harness and real PostgreSQL. Pin versions only after resolving/installing
compatible SDK releases in the execution test environment; record exact versions.

- [ ] Write one real SDK contract test before enabling each matrix cell. Example:

```python
import pytest
from openai import AsyncOpenAI

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_openai_sdk_reads_chat_stream(sdk_server):
    async with AsyncOpenAI(base_url=sdk_server.base_url + "/v1",
                           api_key=sdk_server.secret) as client:
        stream = await client.chat.completions.create(
            model="test-model", messages=[{"role": "user", "content": "Hi"}],
            max_tokens=8, stream=True,
        )
        chunks = [chunk async for chunk in stream]
        assert any(c.choices and c.choices[0].delta.content for c in chunks)
```

- [ ] Run each SDK suite with production inbound codecs against synthetic upstream
  fixtures. `sdk_server` owns temporary gateway server, test customer secret and
  teardown; loopback allowance is scoped to that server, never provider domains.
  Anthropic native SDK checks event types/tools; Google GenAI checks SSE/actions;
  Ollama SDK checks authenticated NDJSON; OpenAI covers Responses too.
- [ ] Add cross-protocol contract tests for every supported text/tool route pair,
  explicit unsupported combinations, schema errors, cache-inclusive usage, counted
  request limits, image capability rejection, provider 429, redirect/DNS attacks,
  cancellation and one reservation shared across all protocols.
- [ ] Run the final verification commands below; capture actual results, no inherited
  test counts. Live smoke requires explicit scoped credentials and token/request
  cap. Missing live keys are a disclosed limitation, not a fake pass or reason to
  run through all saved accounts.
- [ ] Request independent whole-branch review with the applicable review skill;
  fix supported findings with RED/GREEN tests. Do not merge or push without user
  direction. Commit tests/docs as `test: verify multi-protocol gateway contracts`.

## Final verification commands

After service/test extras are installed and the dedicated PostgreSQL test server is
configured, run separately so failures are attributable:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/gateway/integration -q -m postgres
.venv/bin/python -m pytest tests/gateway/sdk -q
.venv/bin/python -m ruff check .
.venv/bin/python -m compileall -q autobuild_json
node --test tests/ui/*.test.mjs
npm run test:browser
npm run test:gateway-browser
.venv/bin/python -m build
git diff --check
```

The first command must not silently skip required database tests in release CI.
Offline-only developer mode may select unit tests explicitly; reports distinguish
unit, PostgreSQL, browser, SDK and live-upstream evidence. Run Python >=3.10 minimum
and the current Python 3.14 environment in CI where dependencies support both.

## Spec coverage and plan self-review

| Spec sections | Owning tasks |
|---|---|
| 1–2, original code/modular architecture | 1, 6, 8, 10, 18–19 |
| 3, customer/key permissions and private admin | 2–4, 10, 17 |
| 4, provider resale/model registry/routing | 5–6, 9, 11–15, 17 |
| 5, four protocol contracts and continuation | 8–14, 19 |
| 6, quota/multipliers/costs/uncertain accounting | 4, 10, 18–19 |
| 7, direct/fixed/pool/KiotProxy | 6–7, 16–17 |
| 8, Codex OAuth/current data/CheckLive boundary | 15–16, 18–19 |
| 9–10, security/streaming/recovery | 2, 6, 8, 10, 18–19 |
| 11, UI/CRUD/playground/audit | 3, 5, 7, 17 |
| 12–13, deploy/regression/verification | 1, 16, 18–19 |
| 14–15, review gates and references | This plan, all adapter tasks, 19 |

Before presenting this plan, verify code-block syntax, file-path ownership, all
referenced records/signatures, five Review Focus tests and no unresolved planning
markers. Before execution, re-read both plan and spec and check changed user files.

## Execution handoff

Recommend **Native** for this plan: one implementer keeps accounting/lease/protocol
interfaces consistent across the dependent tasks, followed by an independent final
review. **Subagent-driven** is an alternative with a fresh implementer/reviewer per
task and higher coordination/context cost. Preserve whichever method the user chooses.

No implementation begins until the user has reviewed this written plan and chosen
the execution method. Approval of the spec has already been recorded separately.
