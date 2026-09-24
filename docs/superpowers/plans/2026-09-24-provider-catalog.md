# Provider & Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admin nối provider, khám phá/sync catalog có provenance, duyệt model atomically và chạy probe có budget mà không làm sai quyền/quota của khách.

**Architecture:** Mở rộng modular FastAPI gateway hiện có bằng domain catalog và durable operation runner dùng PostgreSQL. Reuse adapter, transport, proxy lease, rate admission và money accounting; discovery không tự publish, probe không dùng customer ledger. Maintenance có worker giới hạn concurrency, public gateway không khởi chạy scheduler.

**Tech Stack:** Python 3.10+, FastAPI/Pydantic, SQLAlchemy async + psycopg, PostgreSQL 17, Alembic, HTTPX/httpcore, JS ES modules/assets local, pytest/Node/Playwright Chrome.

**Spec:** [2026-09-24-provider-catalog-design.md](../specs/2026-09-24-provider-catalog-design.md), approved qua “tiếp” ngày 2026-09-24. Plan này **chờ review**, chưa được thực thi.

## Global Constraints

- “Giữ codebase Python/FastAPI riêng, PostgreSQL có thẩm quyền, UI private tiếng Việt, tất cả chế độ direct/fixed/pool/KiotProxy.”
- “Không sao chép source hay triển khai FreeLLMAPI thành sidecar; không suy quyền resale hoặc ‘miễn phí’ từ catalog.”
- Tối đa 10 trang, 10.000 ID duy nhất, 2 MiB JSON/trang và 8 MiB toàn operation.
- Deadline tổng `min(provider.timeout, 60 giây)`, tính cả đợi proxy và các trang.
- Cursor tối đa 2.048 ký tự, không control character; ID upstream 1–200 ký tự.
- Một lệnh publish tối đa 100 lựa chọn; snapshot latest complete, không stale, chưa quá 24 giờ.
- Probe: prompt `Reply with OK.`, output cap 16 tokens, một attempt, không tools/continuation/Codex OAuth.
- Schedule mặc định tắt; bật mặc định 24 giờ, range 5 phút–7 ngày; jitter 0–60 giây; backoff 5/15/60 phút.
- Một source chỉ một queued/running operation; hai job hoạt động mỗi worker process; queued TTL 10 phút; lease grace 15 giây.
- Snapshots 30 ngày/tối đa 20 complete snapshots/source, trừ latest/previous và records còn tham chiếu; accounting/audit không auto-delete.
- Backup 64 MiB; public protocol/schema không đổi; migration không bật schedule hay sửa policy/model/binding cũ.
- Không account/key thật, không live probe, production migration/restart hoặc mở cổng. Tests dùng temp PostgreSQL/synthetic I/O.
- Không thêm Redis/broker/runtime dependency. Python 3.10 không có `asyncio.TaskGroup`: dùng task set + `gather` có cleanup đầy đủ.
- Nhánh `feat/provider-catalog` trong worktree sẵn có; không gộp vào PR #1 hoặc merge/push khi chưa có lựa chọn bàn giao.

## Review Focus

1. Cùng operation ID gửi lại sau completion hoặc ID thứ hai từng được coalesce: không được charge/probe lần nữa — Tasks 4, 11.
2. Rotate credential/đổi giá hoặc effective proxy khi queued/đang phân trang: từ chối stale, không chuyển secret sang host mới — Tasks 3, 6, 7, 10.
3. Trang đầu hợp lệ, trang cuối bị 429/lỗi/cursor loop: giữ snapshot cũ, không kết luận model missing — Tasks 2, 5–7.
4. Crash giữa reserve→dispatch→persist usage→settle và admin reconcile chạy đồng thời: không mất hold hoặc settle hai lần — Tasks 10–11.
5. Publish bị race với manual edit và `all_models` key; cursor admin đọc snapshot cũ: không nới quyền/sửa giá ngầm, không lẫn trang hai snapshot — Tasks 5, 8, 12, 14.

## Workspace, baseline và bằng chứng

Working directory cho mọi lệnh bên dưới:
`/home/mquangprovip0503/Work/Code/AutoBuildJsonCockplit-Tools/.worktrees/api-gateway`.
Branch `feat/provider-catalog` dựa trên `20a1caf`; spec commit `ee2bebc`.
Worktree name lịch sử vẫn là `api-gateway`, không tạo worktree thứ hai hoặc xóa venv.
Đọc `AGENTS.md` nếu xuất hiện mới. Không đọc ignored account/data/export để làm test.

Trước task code đầu tiên, detect worktree theo skill, kiểm tra clean tree và chạy
full baseline `.venv/bin/python -m pytest -q`. Checkpoint trước plan là 549 tests;
đây không thay cho kết quả baseline mới. Runtime test database do fixtures sở hữu.

Mỗi task: RED phải fail đúng hành vi; GREEN phải chạy cả file liên quan; full suite
trước mỗi commit implementation và ghi mọi failure theo tên. Không ghi trước số test
cuối. Chạy full Python 3.10/3.14 + UI/build ở Task 15. Commit tác vụ bằng:

```bash
git -c user.name=Codex -c user.email=codex@localhost commit -m 'message shown in task'
```

Chỉ stage files liệt kê trong task, không `git add .` hoặc chạy test against DB thật.
Đường dẫn rút gọn `catalog/`, `providers/`, `transport/`, `storage/`, `admin/` và
`metering/` trong task đều tương đối với `autobuild_json/gateway/`; `unit/` và
`integration/` tương đối với `tests/gateway/`. File tests Python thêm `import pytest`,
`import asyncio`, `from decimal import Decimal` khi ví dụ dùng chúng. Integration
module khai báo `pytestmark=[pytest.mark.postgres, pytest.mark.asyncio]`; unit async
test dùng marker asyncio. Không dùng pytest gọi thật network ngoài fixture guard.

## File map và dependency order

| File/module | Trách nhiệm | Task |
|---|---|---|
| `catalog/records.py`, `catalog/digests.py` | DTO, version stamp, canonical digest | 1 |
| `providers/presets.py` | Năm preset dữ liệu cục bộ | 1 |
| `providers/discovery/{records,parsers,pagination}.py` | Parse mode, metadata, query chuẩn | 2 |
| `transport/http.py`, `transport/discovery.py` | Request-kind allowlist, đọc JSON có cap | 2, 6 |
| `storage/migrations/versions/0010_provider_catalog.py` | Sources/operations/snapshots/decisions/claims/publications | 3 |
| `catalog/sources.py`, `catalog/context.py` | Source CRUD, config snapshot/locks | 3 |
| `catalog/operations.py` | Idempotency, claim/fence/cancel/terminal writes | 4 |
| `catalog/{snapshots,decisions,cursors}.py` | Immutable commit/diff, ignore, admin paging | 5 |
| `providers/discovery/client.py` | Bounded multi-page HTTP/first-page check | 6 |
| `catalog/runner.py` | Ownership heartbeat, dispatch, cleanup | 7 |
| `catalog/publish.py`, `providers/registry_writes.py` | Atomic model/binding publication | 8 |
| `catalog/{scheduler,worker,retention}.py` | Due claim, task pool, crash repair, retention | 9 |
| `catalog/{probe_quote,probe_accounting,probe}.py` | Quote/hold/execution/evidence/reconcile | 10–11 |
| `admin/{catalog_schemas,catalog_routes}.py` | Private API, legacy wrapper | 12 |
| `admin/static/catalog-{state,view,forms}.js` | New catalog panels in existing UI | 14 |
| `tests/gateway/catalog_support.py` | Synthetic test builders only | 3 onward |

Task 1→2→3→4→5→6→7→8→9→10→11→12→13→14→15 is the default execution order.
Parser tests and schema tests are independently reviewable; no parallel edits to
shared records/schema/runner without explicit delegation. Task 13 is the UI design
gate, not permission to redesign existing screens. Backend Tasks 1–12 can finish
before new UI draft approval.

## Shared contracts (defined by the owning tasks)

Task 1 exports frozen strict Pydantic records; monetary values use `Decimal` in
Python and exact decimal strings on admin wire. No secrets in repr or serialized
context. `actor` is the existing authenticated admin identity, not a body field.

| Record | Fields used by subsequent tasks |
|---|---|
| `SourceInput` | provider_id, credential_id, mode, enabled=True, schedule_enabled=False, interval_seconds=86400 |
| `SourceView` | id + SourceInput + version, next_run_at, latest_successful_run_id, previous_successful_run_id |
| `ConfigStamp` | source_version, provider_version, credential_version, proxy_id/version nullable, content_digest; probe-only binding_id/version, budget_id/version |
| `SourceContext` | source:SourceView, stamp:ConfigStamp, provider:ProviderConfig, route:OperationRoute, effective_proxy_id; no ciphertext/secret |
| `OperationRoute` | provider_id, credential_id, root, adapter, wire_api, auth_mode, config_version, proxy_profile_id, timeout |
| `OperationRequest` | id:UUID client idempotency ID, source_id, kind=`discover/check/probe`, expected:ConfigStamp, probe:ProbeIntent or None |
| `OperationView` | canonical id, source_id, kind, state, version, created_at, deadline nullable, cancel_requested, dispatched_at nullable, usage/cost nullable, result default {}, error sanitized |
| `Claim` | operation_id, source_id, owner, generation, deadline; frozen |
| `ObservedValue` | value scalar/list/object or None, source=`upstream`, path or None, observed_at |
| `CatalogEntry` | upstream_id, metadata:dict[str,ObservedValue]; no raw body |
| `SnapshotItem` | upstream_id, entry:CatalogEntry, diff=`added/changed/missing/unchanged`, decision dict nullable |
| `SnapshotPage` | run_id, previous_run_id, source_changed, items:tuple[SnapshotItem,...], next_cursor nullable |
| `ProbeIntent` | binding_id, quote_digest, expected stamp, max_hold:Decimal, currency, acknowledged:Literal[True] required (no default) |
| `ProbeQuote` | stamp, input_bound, output_bound=16, cost_schedule, upper_cost, currency, digest |
| `PublicationRequest` | id:UUID, source_id, run_id, expected stamp, selections:tuple[PublishItem,...] |
| `PublishItem` | upstream_id, public_model_id, identity, input/output_bound, capabilities, binding_enabled=False, new_model:ModelConfig or None, expected_model_version nullable, binding_id/version nullable, decision_version nullable |
| `DecisionEdit` | upstream_id, ignored:bool, expected_version nullable (None means absent) |
| `PublicationResult` | operation_id, run_id, bindings:tuple of upstream/public/binding IDs, versions |

Gateway dependencies retained: `Database`, `Vault`, `ProfileStore`, `ProxyManager`,
`ProviderLimits`, `BudgetService`, `ProviderConfig`, `ModelConfig`, `BindingConfig`,
`RouteSnapshot`, `InferenceRequest`, `GenerationOptions`, `EventCollector`, `Usage`,
`CostSchedule`, `GatewayError`, `UpstreamRejected`. Import their existing modules;
do not duplicate accounting or protocol contracts.

### Transaction and locking rules

- Source configuration and job locks: provider→credential→effective proxy profile
  →source→operation; lock UUID sets sorted. Do not hold these locks during HTTP.
- Publisher then locks public-model rows sorted by model ID, bindings sorted UUID,
  decisions sorted upstream ID. For absent model/alias IDs use transaction advisory
  lock keyed by normalized public ID, and make manual registry writes use the same
  lock before their insert/update. Check alias conflicts in that transaction.
- Job finalization checks `(id,owner,generation,state=running)` in its UPDATE and
  source/context fingerprint under locks; zero updated rows is lost ownership.
- Budget paths lock operation first, then budget then reservation; inference
  customer paths do not lock provider_operations, so no reverse cycle is added.
- Disable after a page's admission cannot retract bytes already sent. Recheck
  before each new dispatch and before snapshot/publish; mark interrupted/stale,
  never claim the upstream definitely did no work after a network timeout.

---

### Task 1: Normalized contracts, semantic digests and local presets

**Files:** Create `autobuild_json/gateway/catalog/__init__.py`, `records.py`,
`digests.py`; create `autobuild_json/gateway/providers/presets.py`;
test `tests/gateway/unit/test_catalog_records.py`, `test_provider_presets.py`.

**Interfaces:** consumes existing strict models/ModelId/Bounds. Produces all records
above; `content_digest(entries: tuple[CatalogEntry,...]) -> str`,
`operation_digest(request: OperationRequest) -> bytes`,
`list_presets() -> tuple[dict,...]`. Create no DB/network state on import.

- [ ] **1. RED:** pin timestamp/order independence and explicit defaults:

```python
def test_snapshot_digest_is_content_not_observation_time():
    from datetime import datetime, timezone
    from autobuild_json.gateway.catalog.records import CatalogEntry, ObservedValue
    from autobuild_json.gateway.catalog.digests import content_digest
    value = ObservedValue(value="Vendor", source="upstream", path="owned_by",
                          observed_at=datetime(2026, 9, 24, tzinfo=timezone.utc))
    a = CatalogEntry(upstream_id="model-a", metadata={"owner": value})
    b = a.model_copy(update={"upstream_id": "model-b"})
    later = value.model_copy(update={"observed_at": datetime(2026, 9, 25, tzinfo=timezone.utc)})
    a2 = a.model_copy(update={"metadata": {"owner": later}})
    assert content_digest((a, b)) == content_digest((b, a2))
    assert content_digest((a,)) != content_digest((b,))
```

- [ ] **2. Run RED:** `.venv/bin/python -m pytest tests/gateway/unit/test_catalog_records.py tests/gateway/unit/test_provider_presets.py -q`; expected missing new contracts/digest, not broken imports of existing code.
- [ ] **3. Implement:** strict/frozen records, enums `openai_single/openai_cursor/anthropic/gemini/ollama`, UTC aware dates, caps from spec, source provider/credential immutable. Canonical JSON is sorted, compact, ASCII-independent, rejects NaN; decimals serialize as decimal strings. Operation digest includes source/kind/expected/probe but excludes client operation UUID, allowing safe active-job coalescing. Content digest excludes observed timestamps and entry order, not field values or field provenance path.

```python
def canonical_bytes(value):
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")
```

Five immutable preset IDs: `custom-openai-chat`, `custom-openai-responses`,
`anthropic-native`, `gemini-native`, `ollama-native`, version 1. Root stays blank
until admin input; no prices/models/secrets/network default. Validate schedule
300–604800s, publication selections 1–100, capabilities explicit (not tools by
default), no user-controlled auth/root in an operation body.
- [ ] **4. GREEN + edge tests:** all presets can construct appropriate ProviderConfig after a root is supplied; mutation of returned preset cannot change next listing; invalid intervals, duplicate selections, unknown fields, decimal overflow and missing probe acknowledgement fail. Run both files then full pytest.
- [ ] **5. Commit:** `feat(catalog): define controlled discovery contracts and presets`.

### Task 2: Mode parsers, metadata provenance and discovery query policy

**Files:** Create `providers/discovery/__init__.py`, `records.py`, `parsers.py`,
`pagination.py`, `transport/discovery.py`; modify `transport/http.py:OutboundRequest`;
modify `errors.py:SAFE_CODES` to add the catalog codes used by these parsers;
tests `unit/test_discovery_parsers.py`, `test_discovery_queries.py`.

**Interfaces:** `parse_page(mode:str, payload:object, observed_at:datetime,
secret:str|None=None) -> DiscoveryPage`; `DiscoveryPage(entries:tuple[CatalogEntry,...],
next_cursor:str|None, complete:bool)`;
`discovery_request(mode:str,cursor:str|None,auth:tuple|None,deadline:float) -> OutboundRequest`.
Extend OutboundRequest with `kind="inference"` and `discovery_mode=None`; defaults
preserve existing call sites. `kind="discovery"` forces GET and fixed models/tags.

- [ ] **1. RED:** distinguish catalog-empty from unreadable and incomplete:

```python
@pytest.mark.parametrize("payload,code", [
    ({"error": "private-error"}, "upstream_error"),
    ({"data": [], "has_more": True}, "catalog_incomplete"),
])
def test_openai_single_page_never_hides_missing_pages(payload, code):
    from datetime import datetime, timezone
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.providers.discovery.parsers import parse_page
    with pytest.raises(GatewayError, match=code):
        parse_page("openai_single", payload, datetime.now(timezone.utc))
```

- [ ] **2. Run RED:** `.venv/bin/python -m pytest tests/gateway/unit/test_discovery_parsers.py tests/gateway/unit/test_discovery_queries.py -q`.
- [ ] **3. Implement:** parser dispatch by explicit mode, no recursive guessing of envelopes or inferred price/capability. Required malformed ID/duplicate conflicting rows fail the whole page; optional malformed metadata stays unknown with a bounded warning code, never influences price/bounds. OpenAI single-page rejects any nonempty pagination indication (`has_more`, `next`, `next_page`, `nextPageToken`); do not follow those URLs. Ollama accepts name/model, rejects disagreement. Gemini strips only one leading `models/`. Validate lengths and control characters before content hashing.

```python
QUERY_KEYS = {
    "openai_single": frozenset(), "openai_cursor": frozenset({"after", "limit"}),
    "anthropic": frozenset({"after_id", "limit"}),
    "gemini": frozenset({"pageToken", "pageSize"}), "ollama": frozenset(),
}
```

Default page size100 for cursor modes; total caps stay10000 IDs/10 pages. Nếu
upstream chỉ trả ít items mà chưa hết sau10 trang, fail explicit cap, không bịa
full catalog. Page size lấy từ contract mode đã kiểm tra tài liệu, không tự nâng
quá giới hạn vendor để đạt10000. Mode
schema demands boolean has_more; true needs nonempty last_id. Gemini missing/null
nextPageToken means complete, invalid type is error. No query duplicates, unknown
query keys or key/token query; cursor max2048 and URL-encode literal text. `alt=sse`
allowed only by existing inference path. Authentication stays in allowed header.
Preserve source path/time for metadata ID/name/owner/created/shutdown, Gemini
inputTokenLimit/outputTokenLimit/supportedGenerationMethods and explicit relay
capabilities/pricing `{currency,unit,input,output}`; unknown formats are not inferred.
Display name ≤200, owner≤200, modality values finite enum, date ISO validated,
limits strict positive integers≤10^9, price finite Decimal≥0 with supported explicit
unit `per_token/per_million_tokens`. Bool is not numeric. Never parse model names.
- [ ] **4. GREEN:** table cases for all five modes, empty, bool/NaN, secret reflection, cursor injection/duplicates, text XSS left inert, malformed optional metadata, unknown price unit, unsupported media, non-ASCII ID and semantic duplicate. Re-read official native pagination docs before implementing each parser; use synthetic recorded shapes with reference URLs, no real responses/tokens. Run affected transport/protocol unit suites and full pytest.
- [ ] **5. Commit:** `feat(catalog): parse bounded provider model pages without inference`.

### Task 3: Additive schema, source configuration and backup support

**Files:** Create `storage/migrations/versions/0010_provider_catalog.py`,
`catalog/sources.py`, `catalog/context.py`, `tests/gateway/catalog_support.py`;
modify `storage/backup.py`, `admin/extra.py` budget listing for version;
tests `integration/test_catalog_schema.py`, `test_catalog_sources.py`,
extend `test_backups.py`.

**Interfaces:** `SourceRepository(db,vault,pepper)`:
`create(SourceInput,actor)->SourceView`, `get(id)->SourceView`, `list()->list[SourceView]`,
`update(id,expected_version,input,actor)->SourceView`, `context(id)->SourceContext`;
`lock_context(session,source_id)->SourceContext` uses same live checks under row locks.
`context_digest(config:dict,pepper:bytes)->str` HMAC over networking identity,
not display labels/schedule. `lock_context` is a SourceRepository method and takes
optional `skip_locked:bool=False` for scheduler selection while preserving lock order.

**Schema decisions (all identifiers below are exact):**

- `catalog_sources`: id UUID PK; provider_id/credential_id FKs, unique pair; mode,
  enabled true, version1; schedule_enabled false, interval_seconds86400,
  next_run_at nullable, failure_count0; latest_successful_run_id and
  previous_successful_run_id nullable; created_at/updated_at UTC.
- `provider_operations`: id UUID PK, source_id FK, kind/state CHECKs, version1,
  payload_digest BYTEA, input JSONB (sanitized command, no secrets), expected JSONB,
  created_at, queued_expires_at, started_at/deadline/finished_at nullable,
  owner UUID, generation BIGINT0, heartbeat_at, cancel_requested false,
  dispatched_at nullable, attempt_id UUID unique nullable, budget_id FK nullable,
  probe_cost_snapshot JSONB nullable, upper_cost NUMERIC(38,12), usage/cost/result JSONB,
  error_code/upstream_status/retry_after nullable, settlement_source nullable,
  reconcile_digest BYTEA nullable, retention_protected false.
- unique partial index `provider_operations_active_source` on source_id where
  state IN ('queued','running'). Cancellation stays a flag while running.
- `provider_operation_claims`: client_id UUID PK, operation_id FK, payload_digest,
  created_at. Needed so a coalesced second ID remains idempotent after job completes.
- `catalog_entries`: run_id FK + upstream_id TEXT PK, metadata JSONB, digest BYTEA.
- `catalog_decisions`: source_id FK + upstream_id PK, version1, ignored false,
  binding_id FK nullable, published_run_id FK nullable, published_digest BYTEA,
  updated_at. Ignore flag is independent of binding so ignore/undo never loses link.
- `catalog_publications`: id UUID PK, source_id/run_id FK, payload_digest BYTEA,
  result JSONB, created_at. Persist successful transactions only; source lock plus
  unique operation ID serialize concurrent replay/conflict.
- Add `upstream_budgets.version INTEGER NOT NULL DEFAULT 1` for quoted config identity;
  spending changes do not bump config version; no new budget editing feature.
- Source latest/previous run FK is DEFERRABLE INITIALLY DEFERRED; verify run belongs
  to source in repository transaction. Index operation(state,created_at),
  operations(source_id,created_at), next_run_at for enabled schedules, entries by
  run/upstream and decisions binding/published_run; FK deletions RESTRICT except
  entries/claims owned by explicitly purged operations.

- [ ] **1. RED:** source creation must not manufacture catalog or schedule:

```python
async def test_new_source_has_no_implicit_schedule_or_snapshot(pg_db):
    from sqlalchemy import text
    from tests.gateway.catalog_support import source_case
    sources, source = await source_case(pg_db)
    assert source.schedule_enabled is False and source.next_run_at is None
    assert source.latest_successful_run_id is None
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM catalog_entries")) == 0
```

- [ ] **2. Run RED:** `.venv/bin/python -m pytest tests/gateway/integration/test_catalog_schema.py tests/gateway/integration/test_catalog_sources.py tests/gateway/integration/test_backups.py -q`.
- [ ] **3. Implement:** migration `down_revision='0009_provider_admission'`; no seed/update to user data. `source_case` test helper creates synthetic provider/key through existing `catalog_case` and this repository, returns `(sources,source)`, optional `mode='openai_single'` updates provider adapter consistently. Full snapshot/operation builders remain test-only. Context loads selected credential membership/enabled and effective proxy, rejects codex; content fingerprint includes root/adapter/wire/auth/credential encrypted-secret HMAC/token_generation/profile ID+config+entry HMAC/mode. Exclude name, observed_at, next_run_at and unrelated label versions; expected probe versions remain strict. Context serialization returns no cipher or digest input.

```sql
UPDATE catalog_sources SET enabled=:enabled,mode=:mode,
  schedule_enabled=:scheduled,interval_seconds=:interval,version=version+1,
  updated_at=clock_timestamp()
WHERE id=:id AND version=:version RETURNING *;
```

On enable/change interval next_run_at=DB now+interval; disabling nulls it. Provider/
credential identity cannot be edited; create new source. Reject wrong adapter/mode,
foreign credential, private-origin unauthorized. Profile version/storage removal
after snapshot is stale, never direct fallback. Update BackupService TABLES with
dependency order + defer source run FK only during same-revision restore; all rows
included and 64MiB retained. No auto-convert `discovered_models` legacy rows.
- [ ] **4. GREEN:** test upgrade from revision0009 in separate validated temp DB; unique pairs; bad provider/credential; direct/no-auth does not decrypt; credential proxy overrides provider; changed secret/root makes digest change but label/schedule doesn't; stale update409; restore circular pointer/ignored decision/probe hold with synthetic rows; revision mismatch refuses. Run full pytest.
- [ ] **5. Commit:** `feat(catalog): persist sources and operation schema with safe restore`.

### Task 4: Durable commands, coalescing, ownership and cancellation

**Files:** Create `catalog/operations.py`; modify `errors.py` for operation safe codes;
test `integration/test_provider_operations.py`.

**Interfaces:** `OperationStore(db,sources)`:
`enqueue(OperationRequest,actor)->OperationView`, `get(client_or_canonical_id)->OperationView`,
`claim(id,owner)->Claim|None`, `heartbeat(claim)->bool`,
`cancel(id,expected_version,actor)->OperationView`,
`finish(claim,state,result=None,error=None)->OperationView`,
`mark_dispatched(claim,attempt_id)->None`. `assert_claim(session,claim)->OperationView`
and `read_claim(claim)->OperationView` validate fence/deadline/cancel for collaborators.
Internal `enqueue_in(session,request,actor,context:SourceContext)->OperationView`
assumes upstream/source locks already held, performs no commit; both enqueue and
scheduler use it. Add safe operation error codes at this task, not only Task12.

- [ ] **1. RED:** use two requests with different client IDs but same payload:

```python
async def test_coalesced_id_stays_claimed_after_completion(pg_db):
    from uuid import uuid4
    from tests.gateway.catalog_support import source_case
    from autobuild_json.gateway.catalog.records import OperationRequest
    from autobuild_json.gateway.catalog.operations import OperationStore
    sources, source = await source_case(pg_db)
    ops = OperationStore(pg_db, sources)
    expected = (await sources.context(source.id)).stamp
    a = OperationRequest(id=uuid4(), source_id=source.id, kind="check", expected=expected)
    b = a.model_copy(update={"id": uuid4()})
    first, second = await ops.enqueue(a, "admin"), await ops.enqueue(b, "admin")
    assert first.id == second.id
    claim = await ops.claim(first.id, uuid4())
    await ops.finish(claim, "succeeded", result={"reachable": True})
    assert (await ops.enqueue(b, "admin")).id == first.id
    assert await ops.claim(first.id, uuid4()) is None
```

- [ ] **2. Run RED:** `.venv/bin/python -m pytest tests/gateway/integration/test_provider_operations.py -q`.
- [ ] **3. Implement:** enqueue source/context locks, check client claim first (digest mismatch409), coalesce active same digest through new claim alias in same transaction; different active payload409. Queued operation canonical ID is first request ID; no retry generation for an expired/rejected ID. Claims survive terminal states. Claim only queued unexpired, increments generation and sets owner/start/deadline from DB time/provider timeout, heartbeat1s; stale config terminal stale without HTTP. `finish` uses compare-and-set fence, makes no secret/error body copies. Cancel queued → cancelled, running → flag+version; usage_pending cancellation leaves evidence/hold intact.

```sql
UPDATE provider_operations SET state=:state,result=CAST(:result AS jsonb),
  error_code=:code,finished_at=clock_timestamp(),version=version+1
WHERE id=:id AND owner=:owner AND generation=:generation AND state='running'
  AND deadline>clock_timestamp() RETURNING id;
```

Deadline-expired repair belongs Task9 under a newer fence; regular stale worker
cannot use repair API. No network retries or customer ledger writes here.
- [ ] **4. GREEN:** `asyncio.gather` two workers enqueue/claim, duplicate ID across source/kind conflicts, active payload conflicts, cancel idempotent, expired queued never dispatched, generation loss rejects every terminal write. Use events, not sleep races; check rows not mock call counts alone. Run full pytest.
- [ ] **5. Commit:** `feat(catalog): enforce durable operation idempotency and fencing`.

### Task 5: Atomic snapshots, stable diffs, decisions and signed cursors

**Files:** Create `catalog/snapshots.py`, `decisions.py`, `cursors.py`;
tests `integration/test_catalog_snapshots.py`, `test_catalog_decisions.py`,
`unit/test_catalog_cursors.py`.

**Interfaces:** `SnapshotStore(db,sources,ops,cursor_key)`:
`commit(claim,context,entries:tuple[CatalogEntry,...])->UUID`,
`page(source_id,*,cursor=None,limit=50,diff=None)->SnapshotPage`.
`DecisionStore(db,sources).apply(source_id,expected_version,edits:tuple[DecisionEdit,...],actor)->list[dict]`.
`encode_cursor(payload:dict,key:bytes)->str`, `decode_cursor(token,key,now)->dict`.

- [ ] **1. RED:** add test helper `snapshot_case(db,entries)` in catalog_support using
Task4 enqueue/claim and SnapshotStore.commit; return `(sources,source,store,run_id)`.

```python
async def test_cursor_does_not_jump_to_a_new_snapshot(pg_db):
    from tests.gateway.catalog_support import snapshot_case, observe
    sources, source, store, run_id = await snapshot_case(pg_db, (observe("a"), observe("b")))
    first = await store.page(source.id, limit=1)
    assert first.run_id == run_id and first.next_cursor
    # A second complete run on the same source is made by this helper.
    from tests.gateway.catalog_support import complete_snapshot
    await complete_snapshot(store, sources, source.id, (observe("z"),))
    second = await store.page(source.id, cursor=first.next_cursor, limit=1)
    assert second.run_id == run_id and second.items[0].upstream_id == "b"
```

Define test helpers `observe(id)->CatalogEntry` (fixed UTC/time and no metadata),
`complete_snapshot(store,sources,source_id,entries)->UUID` using store.ops and
fresh source stamp; no production test-only hooks.
- [ ] **2. Run RED:** `.venv/bin/python -m pytest tests/gateway/integration/test_catalog_snapshots.py tests/gateway/integration/test_catalog_decisions.py tests/gateway/unit/test_catalog_cursors.py -q`.
- [ ] **3. Implement:** snapshot commit locks context/source/operation, verifies fence and content fingerprint, inserts all normalized entries, shifts previous/latest and sets succeeded in same transaction. Identical entries still produce a complete run but unchanged diff; previous different fingerprint sets source_changed and no missing inference. Diff SQL full join by upstream ID/digest; apply decisions separately. Ignore flag persists even if entry absent and never disables an existing published binding.

```python
# Cursor payload, signed using vault.derive_key("client_keys", "catalog-cursor-v1"):
payload = {"v": 1, "source": str(source_id), "run": str(run_id),
           "previous": str(previous_id) if previous_id else None,
           "after": last_upstream_id, "filter": diff_filter, "exp": expires_epoch}
```

TTL15min, max4096 encoded chars, constant-time HMAC check before accepting JSON,
source/run/filter bindings verified; cursor never contains upstream pagination
tokens. Snapshot page includes source decision version and nullable cursor, sorted
upstream ID; limit1–200. Unknown fields invalid; tampered/expired cursor400, purged
snapshot409 with safe `catalog_snapshot_stale`. Audit decision changes in transaction.
- [ ] **4. GREEN:** all diffs, later observations same digest, failure leaves pointer, decision ignore/undo across disappear/reappear/restart, stale config commit, cursor cross-source/filter replay, sensitive metadata blocked, counters50/200; atomic rollback after insert failure. Run full pytest.
- [ ] **5. Commit:** `feat(catalog): stage immutable snapshots and persistent review decisions`.

### Task 6: Bounded discovery/check HTTP with sticky proxy and admission

**Files:** Create `providers/discovery/client.py`; modify `transport/discovery.py`,
`transport/http.py`; tests `unit/test_discovery_client.py`,
`integration/test_discovery_proxy.py`.

**Interfaces:** `DiscoveryClient(transport,limits,credential_resolver)` where resolver
is existing `Engine.credential(route)->str` (never invoked for auth none).
`collect(context:SourceContext,lease:ProxyLease,check_current:Callable[[],Awaitable[None]],
deadline:float)->tuple[CatalogEntry,...]`;
`check(context,lease,check_current,deadline)->dict` returns reachable,
catalog_readable, authentication=`unverified`, status_code, duration_ms.
`read_discovery_json(response:UpstreamResponse,remaining_bytes:int)->tuple[object,int]`.

- [ ] **1. RED:** mock only external HTTP transport, no mocked parsers/admissions.

```python
async def test_later_429_discards_the_collected_prefix(pg_db):
    import httpx
    from tests.gateway.catalog_support import discovery_case
    from autobuild_json.gateway.errors import GatewayError
    async def upstream(request):
        if request.url.params.get("after") == "first":
            return httpx.Response(429, headers={"Retry-After": "30"})
        return httpx.Response(200, json={"data": [{"id": "first"}],
                                         "has_more": True, "last_id": "first"})
    async with discovery_case(pg_db, upstream, mode="openai_cursor") as case:
        with pytest.raises(GatewayError, match="rate_limited"):
            await case.client.collect(case.context, case.lease, case.check_current, case.deadline)
        assert case.request_paths == ["/v1/models", "/v1/models"]
```

Define test-only `discovery_case` async contextmanager: use source_case, real
Transport(EgressPolicy synthetic resolver,httpx.MockTransport(upstream)), real
ProviderLimits and ProxyManager(PgLeaseStore), one lease with UTC deadline60s;
check_current refreshes source context and compares digest. Expose client/context/
lease/deadline and normalized request paths (not secrets/URLs).
- [ ] **2. Run RED:** `.venv/bin/python -m pytest tests/gateway/unit/test_discovery_client.py tests/gateway/integration/test_discovery_proxy.py -q`.
- [ ] **3. Implement:** total body accounting uses bytes after decompression, page
loop has seen cursors/IDs, reads ≤min(2MiB,remaining_total), parse strict JSON rejecting
duplicate object keys/nonfinite numbers, bounded nesting (reject >32 levels before
normalization). More pages at cap →catalog_limit_exceeded, no prefix return.
Keep credential resolved once and same lease/root; check_current and admission
before each HTTP. Check parses only first page and ignores need for subsequent pages
as evidence of authentication; no snapshot, no fake inference verification.

```python
# Entry loop after read/parse of a successful page:
for entry in page.entries:
    previous = collected.get(entry.upstream_id)
    if previous is not None and content_digest((previous,)) != content_digest((entry,)):
        raise GatewayError("catalog_conflict", 502, "upstream")
    collected[entry.upstream_id] = entry
    if len(collected) > 10000:
        raise GatewayError("catalog_limit_exceeded", 502, "upstream")
```

For discovery requests Transport uses its guarded AsyncBaseTransport directly
with explicit timeout extensions instead of AsyncClient's URL INFO logger;
same TLS/IP pinning, proxy transport, no redirects and normalized exceptions.
No global logging suppression. Test DEBUG caplog with hostile query/credential,
and both raw/compressed size caps. Header429 parse via existing retry.py; store
provider-wide cooldown through runner. Upstream401/403 expose safe rejection
code/status only, not admin401. No secret reflection persisted; no silent direct.
- [ ] **4. GREEN:** all modes' full pagination, repeated cursor, 11 pages/10001 IDs,
8MiB total/2MiB page, redirect with malicious origin, body decompression expansion,
bad nested payload, no-auth resolver not called, credential proxy precedence,
Kiot current exactly once and no out/new from429, pool endpoint stable across
pages, admissions count matches GET count and no active rows after failure.
Run full pytest including egress/transport/proxy regressions.
- [ ] **5. Commit:** `feat(catalog): collect bounded model pages through guarded provider egress`.

### Task 7: Fenced operation runner, cleanup and snapshot commit

**Files:** Create `catalog/runner.py`; tests `integration/test_catalog_runner.py`.
**Interfaces:** `OperationRunner(sources,ops,snapshots,discovery,proxies,profiles,
catalog,probe=None)`; `run(operation_id:UUID)->OperationView` claims a queued job
or returns current status; `execute(claim:Claim)->OperationView` executes owned
work for worker/legacy wrapper. Probe None refuses kind probe until Task11, not
fake success. Dependencies explicitly injected, no background task in constructor.

- [ ] **1. RED:** test runner commits only complete current-source data:

```python
async def test_config_change_after_first_page_prevents_next_dispatch(pg_db):
    from tests.gateway.catalog_support import runner_case
    async with runner_case(pg_db, mode="openai_cursor") as case:
        case.pause_after_first_page.set()
        task = asyncio.create_task(case.runner.run(case.job.id))
        await case.first_page_entered.wait()
        await case.sources.update(case.source.id, case.source.version,
            case.source_input.model_copy(update={"enabled": False}), "admin")
        case.release_first_page.set()
        result = await task
        assert result.state == "stale"
        assert case.sent_pages == 1
        assert (await case.sources.get(case.source.id)).latest_successful_run_id is None
```

`runner_case` test utility assembles source/ops/snapshots/discovery with real DB,
events in synthetic transport only; yields source_input,job,runner,sources, sent_pages
and the three event gates above. Default first response is page1 has_more and
second response completes; non-paused mode returns both immediately.
- [ ] **2. Run RED:** `.venv/bin/python -m pytest tests/gateway/integration/test_catalog_runner.py -q`.
- [ ] **3. Implement:** load expected context under claim, one proxy lease for
remaining whole deadline; heartbeat every1s reads fence/cancel/enabled and cancels
owner task on loss. `asyncio.wait_for` total remaining bound encloses network,
separate cleanup budget5s. Record success only after network/admission/proxy cleanup
finishes; use snapshots.commit for discover or ops.finish for check. Cancellation
or deadline/lost lease cannot write success; recovery owns expired fence.

```python
async def check_current():
    await ops.read_claim(claim)
    current = await sources.context(claim.source_id)
    if current.stamp.content_digest != context.stamp.content_digest:
        raise GatewayError("catalog_snapshot_stale", 409)
```

Persistence helper supports terminal error write after operation deadline only
through fenced recovery, not ordinary finish. A failed durable write must leave
running row recoverable, not return succeeded. Catch only normalized errors;
unexpected exceptions map internal_error without raw string. Record upstream429
receipt time and persist provider cooldown with started_at. Never clear OAuth health.
- [ ] **4. GREEN:** disable/rotate/root/profile changes at each page boundary and
before commit; cancel queued/running; lease loss aborts active HTTP before slot
release; terminal-write DB failure; restart recovery sees no snapshot; browser
disconnect does not affect durable job; two simultaneous runner claims produce one
HTTP operation. Verify orphan tasks count and released PG slots; run full pytest.
- [ ] **5. Commit:** `feat(catalog): run discovery jobs with fenced ownership and cleanup`.

### Task 8: Atomic publish and manual-write conflict protection

**Files:** Create `catalog/publish.py`, `providers/registry_writes.py`;
modify `providers/catalog.py:put_model/put_binding/put_alias`,
`admin/routes.py:update_model/update_binding/delete_model` to use shared locks;
tests `integration/test_catalog_publish.py` and existing `test_admin.py`.

**Interfaces:** `Publisher(db,sources).publish(PublicationRequest,actor)->PublicationResult`;
`registry_writes.lock_model_names(session,names:Iterable[str])->None`,
`write_model(session,model,*,expected_version:int|None,create_only:bool)->int`,
`write_binding(session,binding,*,expected_version:int|None,create_only:bool)->int`.
Existing Catalog methods keep their public signature and wrap these helpers in
their own transaction; publisher passes its transaction (never nested commits).

- [ ] **1. RED:** demonstrate whole-batch rollback with a valid item then conflict:

```python
async def test_publish_conflict_cannot_leave_half_a_batch(pg_db):
    from tests.gateway.catalog_support import publication_case
    from autobuild_json.gateway.errors import GatewayError
    from sqlalchemy import text
    case = await publication_case(pg_db)
    # Helper creates complete snapshot a/b, existing public ID 'occupied', and
    # request selections new 'fresh' then create-only 'occupied' (expected absent).
    with pytest.raises(GatewayError, match="catalog_conflict"):
        await case.publisher.publish(case.conflicting_request, "admin")
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM public_models WHERE id='fresh'")) == 0
        assert await session.scalar(text("SELECT count(*) FROM catalog_publications")) == 0
```

- [ ] **2. Run RED:** `.venv/bin/python -m pytest tests/gateway/integration/test_catalog_publish.py tests/gateway/integration/test_admin.py -q`.
- [ ] **3. Implement:** lock source/live config and latest complete run, age24h by
DB time, active selected credential; guard snapshot digest independent of label/
schedule version. Check operation replay with exact payload digest, then all
selection expected versions. Acquire model-name advisory locks before insert and
same for aliases/manual APIs; sorted rows avoid deadlocks. Validate bounded
`Bounds`, capabilities intersection with implemented adapter contract, correct
identity, source credential, no autodetected tools/vision/media.

```sql
INSERT INTO catalog_publications(id,source_id,run_id,payload_digest,result)
VALUES (:id,:source,:run,:digest,CAST(:result AS jsonb));
```

Insert publication receipt, model/binding/decision/audit in same transaction, all
or none; missing row expected_version=None means create, never upsert overwrite.
Attach to existing model does not change its coefficients/enabled/router_model.
Enabled and capabilities must match explicit selections. Record ignore false only
on explicit publish of that ignored entry. Do not modify api_keys/quota_buckets.
Aliases remain explicit manual configuration. Malformed duplicate selected public
IDs in one batch fail before mutation.
- [ ] **4. GREEN:** two publishes collide; same ID replay no duplicate audit/binding;
same ID different body409; model/alias same-name race; manual binding change race;
stale/proxy-changed/old/nonlatest snapshots; unknown media reject; disabled stays
disabled unless explicit enable; all_models sees newly enabled public model while
specific allowlist does not. Check customer coefficients and upstream pricing
unchanged byte-for-byte. Run full pytest.
- [ ] **5. Commit:** `feat(catalog): publish reviewed mappings atomically without policy drift`.

### Task 9: Scheduler, worker pool, expired-job recovery and retention

**Files:** Create `catalog/scheduler.py`, `worker.py`, `retention.py`;
modify `maintenance.py:run`, `__main__.py:maintenance`, `admin/services.py:close`;
tests `integration/test_catalog_scheduler.py`, `test_catalog_recovery.py`,
`test_catalog_retention.py`, `unit/test_catalog_worker.py`.

**Interfaces:** `Scheduler(db,ops,sources,jitter=random.randint)`:
`tick()->list[UUID]`, `completed(operation_id)->None`;
`CatalogWorker(db,runner,scheduler,probe_accounting=None).run(stopped:asyncio.Event)->None`,
`repair_expired(limit=100)->int`; `Retention(db).purge(now,limit=100)->dict`.
Maintenance accepts optional worker; `Maintenance(db)` legacy behavior remains.

- [ ] **1. RED:** two schedulers must not duplicate a due source:

```python
async def test_two_schedulers_claim_one_due_job(pg_db):
    from sqlalchemy import text
    from tests.gateway.catalog_support import source_case
    from autobuild_json.gateway.catalog.operations import OperationStore
    from autobuild_json.gateway.catalog.scheduler import Scheduler
    sources, source = await source_case(pg_db)
    async with pg_db.sessions.begin() as session:
        await session.execute(text("UPDATE catalog_sources SET schedule_enabled=true,next_run_at=now() WHERE id=:id"), {"id": source.id})
    ops = OperationStore(pg_db, sources)
    a, b = Scheduler(pg_db, ops, sources), Scheduler(pg_db, ops, sources)
    await asyncio.gather(a.tick(), b.tick())
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM provider_operations WHERE state='queued'")) == 1
```

- [ ] **2. Run RED:** `.venv/bin/python -m pytest tests/gateway/integration/test_catalog_scheduler.py tests/gateway/integration/test_catalog_recovery.py tests/gateway/integration/test_catalog_retention.py tests/gateway/unit/test_catalog_worker.py -q`.
- [ ] **3. Implement:** read candidate due source IDs without row locks first, then
`sources.lock_context(session,id,skip_locked=True)` acquires provider/credential/
profile/source in the global lock order; recheck due predicate under source lock.
Skip locked sources instead of reversing source→provider order. Enqueue through
OperationStore.enqueue_in and advance next_run_at in the same transaction.
No lock→second-transaction enqueue gap. Skip busy/disabled source/provider, pending
probe isn't active but its budget remains held. Failed schedule next time:

```python
backoff = (300, 900, 3600)[min(max(failure_count, 1), 3) - 1]
next_run_at = max(now + timedelta(seconds=interval_seconds),
                  now + timedelta(seconds=backoff), cooldown_until or now)
next_run_at += timedelta(seconds=jitter(0, 60))
```

Success resets failure_count; no backlog replay. Worker task set≤2, claim oldest
queued, 1s bounded stopped.wait loop; maintenance recovery still runs every15s
without nesting jobs in its30s lease. Shutdown cancel tasks, await cleanup≤5s,
leave uncertain rows repairable. No unawaited fire-and-forget, no TaskGroup3.11.
Repair queued after10min→cancelled; running only after deadline+15s bumps fence,
nonprobe→failed/interrupted, probe delegates Task11 recovery, never requeues it.
Before Task11 reject probe enqueue at API layer; worker missing handler fails
provably undispatched probe without money writes.
Retention protects latest/previous, decision/publication-referenced run IDs and
all probes; delete unreferenced oldest runs beyond20 or age30d in batches≤100,
associated entries/claims in transaction. Do not cut accounting history to fit
backup cap. Return `protected_over_limit` counts for source status.
- [ ] **4. GREEN:** injected DB clock/test row dates not real sleep; schedule off
makes zero calls; busy-source skip, long cooldown+backoff, failure isolated per
source, stale claim repair, terminal probe excluded from purge, cursor purged409,
worker max2 and maintenance tick responsive while a job stalls; SIGTERM cleanup.
Run full pytest. Add lock-order regression running scheduler and manual source
update/publish behind event gates; bounded wait must complete without deadlock.
- [ ] **5. Commit:** `feat(catalog): schedule bounded discovery jobs and recover stale work`.

### Task 10: Probe quote and atomic internal accounting primitives

**Files:** Create `catalog/probe_quote.py`, `probe_accounting.py`;
modify `metering/budgets.py` to expose session-aware methods without changing
existing methods' behavior, `errors.py` for probe_budget_required;
tests `integration/test_probe_accounting.py`,
`unit/test_probe_quote.py`, existing `test_upstream_budget.py`.

**Interfaces:** `ProbeQuotes(db,sources).quote(source_id,binding_id)->ProbeQuote`;
`upper_cost(input_bound:int,schedule:CostSchedule)->Decimal`;
`ProbeAccounting(db,ops,budgets)`:
`reserve(claim,quote)->UUID`, `mark_dispatched(claim)->None`,
`record_evidence(claim,usage:Usage|None,outcome:str)->None`,
`settle(operation_id,*,claim:Claim|None=None)->OperationView`,
`reconcile(operation_id,expected_version,actual:Decimal,currency:str,reason:str,actor)->OperationView`.
BudgetService adds `reserve_in(session,budget_id,attempt_id,currency,amount,request_id=None)`
and `settle_in(session,attempt_id,actual)`; public reserve/settle wrap them. This
lets reserve+operation link and settle+audit be indivisible for probes.

- [ ] **1. RED:** exact reservation and customer-ledger isolation:

```python
def test_probe_upper_cost_is_exact_and_rounded_up():
    from decimal import Decimal
    from autobuild_json.gateway.catalog.probe_quote import upper_cost
    from autobuild_json.gateway.metering.costs import CostSchedule
    schedule = CostSchedule("USD", Decimal("1"), Decimal("3"))
    assert upper_cost(1000, schedule) == Decimal("0.001048000000")
```

Integration test reserves a quote with input_bound1000/output16 then asserts
upstream budget held0.001048, customer requests/quota_buckets/usage_ledger unchanged;
transaction failure while linking operation must leave both held and reservation0.
- [ ] **2. Run RED:** `.venv/bin/python -m pytest tests/gateway/unit/test_probe_quote.py tests/gateway/integration/test_probe_accounting.py tests/gateway/integration/test_upstream_budget.py -q`.
- [ ] **3. Implement:** quote validates source credential+binding/adapter/output≥16,
finite positive input bound, enabled provider+credential, cost schedule/budget
currency. Binding may be a disabled draft (explicit probe) but existing quarantined
usage-bound failure not automatically re-enabled. Quote digest over all expected
config/binding/budget versions, cost snapshot, max hold, fixed prompt/cap; no secret.
No network or budget reserve from quote. Exact arithmetic:

```python
def upper_cost(input_bound, schedule):
    from decimal import Decimal, ROUND_CEILING, localcontext
    with localcontext() as ctx:
        ctx.prec = 80
        rates = [schedule.input_per_million]
        rates += [rate for rate in (schedule.cache_read_per_million,
                                    schedule.cache_write_per_million) if rate is not None]
        amount = (input_bound * max(rates) + 16 * schedule.output_per_million) / Decimal(1000000)
        return amount.quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING)
```

Reserve rechecks full quote, `max_hold == quote.upper_cost`, same currency and
both expected stamps equal the live quote; acknowledgement must be explicit true.
Transaction creates
attempt_id on operation and reservation with request_id NULL, reserve money via
reserve_in. Mark dispatched persisted before HTTP; no replay after it even if a
crash occurred before actual write (conservative ambiguity). Evidence+status in
same operation row; record outcomes success/rejected/uncertain, never raw body.
Missing cache cost rate can make actual uncomputable despite usage: pending rather
than guessed0; UI reconcile supports money-only evidence with reason, no fake usage.
Settlement source provider_estimated/admin_adjusted distinct, decimal≤12 places,
large actual may exceed hold but is recorded+incident not clamped. Reconcile digest
per operation: same inputs replay, different inputs409, only usage_pending.
- [ ] **4. GREEN:** foreign binding, unsupported Codex, output15, missing cost,
unknown currency, nonfinite money, quote stale after cost/key/proxy change, two
reserves and two settles, zero explicitly priced route, incomplete cache pricing,
reconcile concurrency/negative/wrong currency. Existing customer accounting tests
unchanged; run full pytest.
- [ ] **5. Commit:** `feat(catalog): reserve and settle probe costs outside customer quota`.

### Task 11: One-attempt inference probes and crash-safe reconciliation

**Files:** Create `catalog/probe.py`; modify `catalog/runner.py`, `worker.py`;
tests `integration/test_provider_probe.py`, `test_probe_recovery.py`.

**Interfaces:** `ProbeService(db,sources,ops,quotes,accounting,adapters,transport,
limits,proxies,profiles,catalog)`;
`execute(claim)->OperationView`, `recover(operation_id)->OperationView`.
Runner probe branch delegates before acquiring discovery proxy to avoid duplicate
leases. Worker repair passes expired operations to recover with a newer fence.

- [ ] **1. RED:** accepted duplicate job cannot make a second generation:

```python
async def test_probe_replay_charges_upstream_once_not_the_customer(pg_db):
    from sqlalchemy import text
    from tests.gateway.catalog_support import probe_case
    async with probe_case(pg_db) as case:
        first = await case.ops.enqueue(case.command, "admin")
        await case.runner.run(first.id)
        replay = await case.ops.enqueue(case.command, "admin")
        await case.runner.run(replay.id)
        assert case.generation_calls == 1
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM requests")) == 0
            assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 0
            assert await session.scalar(text("SELECT actual FROM upstream_reservations")) == Decimal("0.000019")
```

`probe_case` synthetic utility: provider/binding input1000/output500, USD input1/
output3, budget1, explicit quote/cap16, existing chat_success usage4/5; real adapter,
transport MockTransport only. Expose context, runner, ops, command and observed
outbound POST count. Never use a stored production credential or injected fake
success at the ProbeService boundary.
- [ ] **2. Run RED:** `.venv/bin/python -m pytest tests/gateway/integration/test_provider_probe.py tests/gateway/integration/test_probe_recovery.py -q`.
- [ ] **3. Implement:** same cleanup pattern as PreparedCall (one shared close task,
shield only bounded cleanup); payload exactly one user Text prompt, options cap16,
no tools/continuation. Build RouteSnapshot from chosen binding; preflight/egress/
quote validate before reserve+dispatch. One pinned proxy lease and one admission;
consume existing canonical adapter events until terminal usage, discard text after
nonempty evidence. Reject unexpected tool/media event as unsupported/uncertain,
never execute it. Save usage evidence before money settlement and safe verification
result, no prompt/output history. Capture429 receipt before cleanup for shared
cooldown. No fallback or retry, even for429; definite rejection returns held0.

```python
# Crash repair truth table; apply while holding operation row/fence:
if operation.dispatched_at is None:
    outcome = "release_unused"
elif operation.result.get("rejected_before_generation"):
    outcome = "release_unused"
elif operation.usage is not None and operation.cost is not None:
    outcome = "settle_evidence"
else:
    outcome = "usage_pending"
```

Record rejection evidence before cleanup; timeout after dispatch conservatively
keeps hold. `usage_pending` releases active/proxy slots but not money; no implicit
requeue. Usage over bound quarantines exact binding under version check; changed
binding doesn't get overwritten, audit incident with original version instead.
Cancelled before dispatch released; after dispatch pending unless definite evidence.
Recovery and reconcile serialize operation/budget/reservation, complete even if
previous process settled money before terminal state (transaction should eliminate
that gap for new probes). Do not mutate OAuth health/provider enabled state.
- [ ] **4. GREEN:** each crash seam (before reserve, after reserve, dispatch mark,
after first chunk, evidence before settle), 429/401/5xx/timeout/truncated/missing
usage/cache price, zero price, output-bound violation, cleanup concurrently called,
two workers+reconciler, browser poll disconnect independent, duplicate alias IDs
after terminal. Fixed/pool/Kiot keep endpoint and release on every outcome. Run
full pytest and targeted suite under Python3.10.
- [ ] **5. Commit:** `feat(catalog): execute bounded probes with durable cost reconciliation`.

### Task 12: Private admin endpoints, exact wire values and legacy discovery

**Files:** Create `admin/catalog_schemas.py`, `catalog_routes.py`;
modify `admin/services.py`, `admin/routes.py:SafeAdminRoute/create_admin_router`,
`admin/actions.py:discover`, `__main__.py:maintenance`, `errors.py`;
tests `integration/test_catalog_admin.py`, `test_catalog_legacy.py`,
extend `test_admin.py`, `unit/test_cli.py`.

**Interfaces:** `mount_catalog_routes(router,services)->None` mounts spec endpoints;
AdminServices has sources, operations, snapshots, decisions, publisher, quotes,
probe_accounting, discovery_client, operation_runner, catalog_worker explicitly
constructed in dependency order. `services.catalog` remains inference Catalog.
Use existing ExactJSONResponse/StrictInput and auth dependency. Add read-only
GET `/catalog/sources/{id}/probe-quote?binding_id={uuid}` to supply the already
required cost preview; GET source list returns recent job ID/status, sanitized
current ConfigStamp and worker availability from a shared heartbeat, no inference/
network call from GET. Disabled source remains listable/editable but enqueue fails;
build source view without using context()'s runnable-enabled guard.

- [ ] **1. RED:** unauthorized/CSRF/cross-listener and upstream-auth distinction:

```python
async def test_catalog_api_stays_private_and_csrf_protected(pg_db):
    from tests.gateway.integration.test_admin import admin_env
    async with admin_env(pg_db) as (client, services):
        assert (await client.get("/api/service/provider-presets")).status_code == 401
        client.headers["x-test-session"] = "synthetic-session"
        assert (await client.post("/api/service/catalog/sources", json={})).status_code == 403
        client.headers["x-csrf-token"] = "synthetic-csrf"
        presets = await client.get("/api/service/provider-presets")
        assert presets.status_code == 200
        assert len(presets.json()["presets"]) == 5
```

- [ ] **2. Run RED:** `.venv/bin/python -m pytest tests/gateway/integration/test_catalog_admin.py tests/gateway/integration/test_catalog_legacy.py tests/gateway/integration/test_admin.py tests/gateway/unit/test_cli.py -q`.
- [ ] **3. Implement:** HTTP201 create source,202 enqueue,200 replay/read/cancel/
reconcile/publish,409 versions/content conflict,422 safe validation. New aliases
and records never serialize repr/cipher/secret/raw exception. SafeAdminRoute only
forwards validated Retry-After, Cache-Control:no-store on catalog responses.
Add safe codes `catalog_incomplete/catalog_limit_exceeded/catalog_snapshot_stale/
catalog_conflict/probe_budget_required/operation_cancelled/operation_expired` to
GatewayError; propagate upstream401/403 as safe upstream rejection under502, not
admin401. Completed async job errors stored and returned in job view.

```python
@router.post("/catalog/sources/{identity}/discover", status_code=202)
async def discover_source(identity: UUID, payload: EnqueueInput):
    command = OperationRequest(id=payload.operation_id, source_id=identity,
                               kind="discover", expected=payload.expected)
    return await services.operations.enqueue(command, "admin")
```

Define `EnqueueInput(operation_id:UUID,expected:ConfigStamp)`, ProbeEnqueueInput
adds ProbeIntent; SourceUpdateInput adds version; Cancel/Reconcile use version;
all unknown fields rejected, no actor/root/secret allowed in commands. Admin
source list supports optional provider filter; entries query validates signed
cursor/snapshot identity, limits50/200; legacy discovered IDs shown as legacy only.

Legacy `POST /providers/{id}/discover {}` chooses enabled credential first by UUID,
create-or-load source without resetting mode/schedule, rejects codex. Use same
source repository and operation runner claim in request; to avoid scheduler race,
`OperationStore.enqueue_claimed(command,owner,actor)->Claim` creates running job
and claim in one transaction using shared enqueue/claim internals. Existing busy
source409. Reply includes models list, published=false, credential_id and run_id from full
snapshot; no worker required. Deadline60s; request disconnect cancels this inline
operation, waits cleanup, no orphan task. Repeated legacy request (no client ID)
is a new read-only discovery, never a probe. Endpoint uses updated `services.transport`
in tests by composing discovery client at operation setup, not stale constructor
reference; do not regress the existing test that overrides services.transport.

CLI maintenance starts worker only on this command, same vault/egress profiles;
`serve`/`admin` startup never schedules discovery or probes. Cancellation gracefully
stops worker and closes services; all dependencies loaded only with service extras.
Worker heartbeat stored in namespaced existing proxy_leases resource with short
deadline, no public endpoint details; read-only GET source status checks live row.
- [ ] **4. GREEN:** every spec route CSRF/auth/validation, exact Decimal/large quotas,
public listener404, probe no ack blocked, async browser disconnect retains queued,
legacy no-worker success/rejection/timeout/collision, upstream401 does not logout,
worker offline returns202 plus queued status (not503 on accepted enqueue),
healthcheck GET never bills. API pagination
stable under sync; codes/hints no secrets. Run full pytest.
- [ ] **5. Commit:** `feat(admin): expose controlled catalog operations and preserve legacy discovery`.

### Task 13: Approve only the incremental Catalog UI design

**Files:** Update `docs/gateway-implementation-decisions.md`; design artifacts in
ignored `.superdesign/` only, no secret/sample production dataset.

**Interfaces:** consumes approved spec and private API from Task12. Produces
documented approved visual draft ID + component/state mapping for Task14. No
product UI code in this task. This is a visual review step, not a new architecture.

- [ ] **1. Inspect existing design context:** read Superdesign skill and its required
references at execution; use approved v3 project `7d62e963-3633-45cd-ae16-6445cd1b4738`,
draft `9da57406-2110-462f-a8ee-0f293bc9c332` as baseline. Check saved worktree context
and current UI rather than regenerating global layout/nav/OAuth screens.
- [ ] **2. Create the incremental design:** provider preset selector, source/credential/
effective-proxy/schedule editor, Catalog list with diff/provenance/unknown, explicit
publish preview, separate check and charge-confirmed probe, operation pending/
reconcile. Model/binding enabled defaults off, tools unselected, all_models warning.
Show queued-worker-offline, stale snapshot, ignored/missing,429+retry delay,
budget-required and uncertain cost; desktop and390px layout using synthetic names.
- [ ] **3. Present draft and request approval:** backend Tasks1–12 remain available
for safe verification while waiting. Do not implement unapproved new UI visual
choices. If tool/approval blocked, report it; no implicit redesign fallback.
- [ ] **4. Record accepted draft and state-to-endpoint mapping:** include approved ID,
acceptance date and differences from baseline, not generated full source. Note
metadata/check success does not mean inference credential verified.
- [ ] **5. Commit:** `docs: record approved provider catalog admin UI extension`.

### Task 14: Implement Catalog admin panels and browser acceptance

**Files:** Create `admin/static/catalog-state.js`, `catalog-view.js`, `catalog-forms.js`;
modify `admin/static/{app,api,forms}.js`, `index.html`, `style.css` as needed for
approved extension; tests `tests/ui/catalog-state.test.mjs`,
`tests/ui/catalog-browser-smoke.mjs`; modify `tests/gateway_ui_server.py`, `package.json`.

**Interfaces:** `createCatalogState()` produces `{selectSource(id),beginLoad(),
acceptLoad(token,data),clear(),snapshot()}`; snapshot returns sourceId/rows/selection/
operation with no secrets. `mountCatalogView({root,api,state,onNotice})->{destroy()}`;
`publicationBody(form,selection,context)->object` and
`probeBody(quote,acknowledged,operationId)->object` preserve exact decimal strings.
Existing createApi receives optional AbortSignal and exposes parsed safe
Retry-After on thrown errors; epoch/logout safety preserved.

- [ ] **1. RED state tests:** stale source response cannot replace current view:

```javascript
import test from 'node:test';
import assert from 'node:assert/strict';
import {createCatalogState} from '../../autobuild_json/gateway/admin/static/catalog-state.js';
test('source switch rejects stale catalog page', () => {
  const state = createCatalogState();
  state.selectSource('source-a');
  const token = state.beginLoad();
  state.selectSource('source-b');
  assert.equal(state.acceptLoad(token, {rows:[{upstream_id:'private-a'}]}), false);
  assert.deepEqual(state.snapshot().rows, []);
});
```

- [ ] **2. Run RED:** `node --test tests/ui/catalog-state.test.mjs`; expected missing
module before implementation. Add browser assertions against real admin/PG fixture
for the approved labels and actions, not substring assertions on source text.
- [ ] **3. Implement:** isolate new panel modules, render upstream text with `textContent`.
Fetch catalog only on selected source/page, not add10k entries to global load().
Stop polling on view change/logout/destroy; poll1s while queued/running and page
visible, on hidden tab stop then refetch when visible. Return errors separately
from empty table. UUID per button operation retained across transport retry,
different after terminal user rerun; probe confirmation never auto-clicked/retried.

```javascript
const blocked = !quote || !acknowledged || running;
probeButton.disabled = blocked;
probeButton.textContent = 'Thử inference — có thể tính phí';
checkButton.textContent = 'Kiểm tra kết nối';
```

Preset fills only pristine editable form (no network), root explicit; credential
selection required, resolved proxy label shown without entries/key. Diff filters,
ignore/undo/publish preview ≤100 selected; version409 reloads preview, never silently
resubmits. New model coefficients entered separately from observed prices;
capability unknown not checked. Probe upper cost/currency exact string, pending
money and reasoned reconcile. Admin all_models warning before enable. Legacy button
still works where present but new panel uses async routes.
Browser fixture starts real worker in test-app lifespan explicitly (production
admin still doesn't), injects synthetic HTTP pages and controllable failure states
through test-only handler. Ensure server-owned worker stops before temp DB teardown.
Add npm `test:catalog-browser` script; all assets included by existing package-data
glob. No external assets/CDN/runtime downloads.
- [ ] **4. GREEN:** `node --test tests/ui/*.test.mjs`, `npm run test:catalog-browser`,
`npm run test:gateway-browser`, `npm run test:browser`. Browser covers preset/source,
credential proxy override, pages/diff/unknown, ignore persists after new sync,
publish explicit/new disabled model, enabled allowlist behavior, schedule/offline,
check vs probe/budget/pending/reconcile, XSS, logout/source stale data,390px. Assert
zero external requests and no storage secrets. Run full pytest too.
- [ ] **5. Commit:** `feat(ui): manage provider catalog review and explicit costed probes`.

### Task 15: Operational docs, full regression, security and handoff

**Files:** Update `README.md`, `CHANGELOG.md`, `docs/gateway-operations.md`,
`docs/gateway-compatibility.md`, `docs/implementation-status.md`,
`docs/gateway-implementation-decisions.md`, `tests/ui/acceptance.md`;
modify `.github/workflows/gateway-tests.yml` only to run new Node tests if script
globs don't already include them. No new production environment/deployment.

**Interfaces:** consumes complete approved branch. Produces reproducible evidence,
review findings/resolution and explicit integration choice; never a fabricated
live compatibility or CI result.

- [ ] **1. Update operating instructions:** migrate additive schema with encrypted
backup first; run maintenance for async jobs; interval/default-off, no scheduled
generation, same proxy config, worker offline queue TTL10min, probe confirmation/
budget/reconcile and declared unknowns. Explain legacy sync wrapper, no auto-enable,
all_models effect and signed cursor freshness. Record no live verification.
Use safe sample `provider.invalid`, synthetic values, no copy from real data.
- [ ] **2. Full verification on final code:**

```bash
.venv/bin/python -m pytest -q
.deps/venv310/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m compileall -q autobuild_json
.venv/bin/python -m build --no-isolation
node --test tests/ui/*.test.mjs
npm run test:browser
npm run test:gateway-browser
npm run test:catalog-browser
git diff --check
```

Packaging smoke: install built wheel to ignored temporary venv and verify new
static modules/imports/preset CLI/admin response with test config, no provider I/O.
CI uses normal isolated build; local `--no-isolation` is existing ensurepip workaround.
- [ ] **3. Independent whole-branch review:** read requesting-code-review skill then
dispatch one reviewer with base `20a1caf`, actual head, approved spec+this plan and
test evidence. Read-only review; no accounts/delegated sub-review. Reproduce every
Critical/Important finding with failing test then fix, run full suites again. Record
minor/out-of-scope limitations explicitly. Check all five Review Focus items.
- [ ] **4. Secret/diff audit and final commit:** inspect exact tracked/staged files,
run local Gitleaks on new commit range with redaction; no broad exclusions. Stage
only intended docs/tests changes then commit `docs: record provider catalog acceptance
and operator workflow`. Do not claim the preexisting549 count is the new result.
- [ ] **5. Handoff integration decision:** preserve worktree/dependencies. If user
selects PR, push this branch only, create new PR targeting agreed base (PR #1 still
unmerged implies a stacked PR against feat/api-gateway or wait for its merge),
clearly describe dependency; never auto-merge PR #1. Watch CI via gh run watch
with short tool yields and user updates. Public deployment/live smoke requires
separate scoped authority; do not infer it from plan approval.

## Coverage / self-review ledger

| Spec section | Plan tasks |
|---|---|
| 1–2 objective/scope/module boundaries | header, file map, 1–15 |
| 3 presets/source/proxy/identity/version | 1,3,6,7,12,14 |
| 4 pagination/metadata/SSRF/no false validation | 2,6,7,12 |
| 5 snapshots/diff/ignore/publish/permissions | 4,5,8,12,14 |
| 6 check/probe/budget/reconcile | 6,10,11,12,14 |
| 7 jobs/schedule/fencing/cancel/recovery | 4,7,9,11,12 |
| 8 schema/migration/retention/backup | 3,4,5,9,10,15 |
| 9 private API/legacy/exact wire | 12,14 |
| 10 approved UI/security/operation semantics | 6,7,12–15 |
| 11 acceptance / matrix | tests in each owning task +15 |
| 12 references/approval | spec, Task2 docs verification, final handoff |

Implementation refinements are not new product scope: operation claim alias table
is required by active coalescing+idempotency; budget version supports approved quote
guard; GET probe-quote supplies existing confirmation UI; deferred source pointer FK
supports valid backup restore. No adapter/client protocol expansion is included.

Self-review completed at planning handoff: all 12 spec sections mapped above,
five Review Focus cases assigned to owner tests, safe error-code dependencies
moved before their first use, required probe acknowledgement has no true default,
and scheduler lock order matches source/publisher order. No product code or test
file was changed in the planning commit; RED/GREEN commands above are instructions
for execution, not test results claimed by this document.

**Execution recommendation:** Native: implement tasks in order in this session,
then one independent whole-branch review. Shared schema/context/fencing/accounting
interfaces make per-task implementer turnover costly. Subagent-driven remains an
option if the user wants an independent review after each task and accepts the
extra context/cost. Neither execution method starts until the user reviews this
plan and selects/confirms it.
