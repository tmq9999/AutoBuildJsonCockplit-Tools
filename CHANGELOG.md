# Changelog

Ghi nhận chức năng và bằng chứng kiểm thử. `Unreleased` không phải GitHub Release
hay cam kết production-ready; version package hiện vẫn là `0.1.0`.

## [Unreleased] — 2026-09-24

### Full gateway review repairs — 2026-09-26

- Follow-up recovery/report review: maintenance no longer spends its bounded
  recovery batch repeatedly revisiting usage-pending requests with no saved
  provider usage; their holds remain pending. Saved usage remains recoverable.
- Proxy cleanup attempts every lease even when one release fails, then reports
  the first cleanup error without swallowing cancellation or timeout behavior.
- Usage report rows and summaries now use the same request-admission window.
  Request trend charts prefer `admitted_at` (with a legacy `started_at`
  fallback), so OAuth refresh/proxy wait time cannot make a charged request
  disappear from the selected report window.
- Post-restart verification on 2026-09-27: 106 real history windows matched
  summary/detail charges; a fresh Codex WebSocket request completed with
  12 input / 7 output, 26 weighted tokens charged and zero held. The 14 old
  pending requests were preserved without invented usage or refunds.

- A Codex HTTP/WebSocket 401 expires only the access token actually rejected,
  enabling refresh after cooldown without invalidating a concurrent newer token.
  Turning off session affinity now also stops using existing header-based pins;
  native continuations remain bound to their original account.
- Responses JSON/SSE retain validated tools, tool choice and parallel-call
  settings. Gemini/Ollama match repeated same-name tool results FIFO instead of
  overwriting the first call; Gemini validates explicit result IDs.
- Report account selectors use stable UUIDs across account pages, merge newly
  visited accounts, and preserve the selection when submitting a saved report.
- Token counting validates egress before dispatch and atomically completes its
  zero-generation usage attempt with settlement. Provider create/update rejects
  nonexistent proxy profiles under a lock that also fences concurrent deletion.
- Image-only Codex bindings no longer implicitly advertise compact/WebSocket.
  Invalid Ollama `/api/show` JSON/model types return a controlled 400.
- Codex WebSocket `codex.rate_limits`, `codex.response.metadata` and
  `responsesapi.websocket_timing` handshake/timing advisories are ignored at
  the adapter boundary; the shared Responses parser remains strict and
  terminal provider usage remains authoritative.
- WebSocket upgrades inspect the first lifecycle frame before exposing the
  stream to the engine. Typed pre-start model/authorization/rate-limit
  rejections can fail over to the next eligible account; errors after
  `response.created` remain non-replayed and usage-pending.
- Public HTTP responses, including model catalogs and errors, are `no-store`.
  Public/admin boundaries reject duplicate Origin headers; trusted proxy config
  rejects wildcards, invalid hosts, noncanonical CIDRs and comma injection.
- After restarting the existing local gateway, four real Codex calls through
  Responses JSON/SSE, Gemini and Ollama completed: 541 input / 44 output tokens,
  exactly 629 weighted tokens charged at input ×1 / output ×2, with zero holds.
  Real Chromium verified the cross-page report selection, first-entry models,
  Playground response and saved dashboard totals. Details and limitations:
  [live verification](docs/live-verification-2026-09-26.md#full-gateway-review-follow-up).
- Real WebSocket generation completed after reproducing and fixing the advisory
  parser failures: 12 input / 7 output, exactly 26 weighted tokens charged,
  zero held on its temporary key. Earlier failed diagnostics retain unknown
  usage/holds on revoked temporary keys; their receipts are not fabricated.
- Follow-up regression review fixed malformed image input and malformed Gemini
  token-count payloads (safe 400/502), fenced token counting after a route is
  disabled while dispatch waits, and shaped SQLAlchemy failures for each public
  protocol instead of returning the wrong OpenAI envelope.
- Final fresh regression suite: 1,779 Python tests and 55 Node tests passed;
  Ruff, JavaScript syntax and diff checks passed (two dependency warnings remain).

### Live thinking, usage and dashboard repair — 2026-09-26

- Fixed account-filtered report summaries after cross-account retries: serving
  usage/charge and current holds no longer leak into a rejected account's totals.
  Verified against six real account histories in the running gateway.
- Playground waits for the current model registry before constructing its form;
  new publications appear on first entry. Added an explicitly opted-in real
  Chromium verification (no upstream mocks, temporary keys revoked afterward).
- Seven fresh real API/browser calls matched immutable PostgreSQL settlements:
  113 tokens charged, zero held, 99,999,887 left on a 100-million-token key.
- Reproduced live failures: Chat rejected `reasoning_effort`, Messages rejected
  `thinking`, and the Codex stream parser rejected a real reasoning item with
  `content: []` before reading terminal provider usage.
- Accept the empty reasoning content envelope without exposing raw reasoning;
  support Chat effort and Messages enabled/adaptive/disabled controls, retaining
  native Anthropic intent while translating Codex effort explicitly.
- Read cache-write usage when present. Share a server-side request/settlement
  summary across dashboard and reports; input/output totals no longer depend
  on log pagination or disappear because a rejected attempt has no usage.
  Cached and reasoning subsets are never added to total tokens again.
- Replace the invalid `test-model` playground default with published model,
  wire API and thinking selectors. Display actual response usage and refresh
  saved totals. Codex overview updates every ten seconds while visible.
- Real HTTP evidence: Responses/Chat high, Responses SSE, Chat SSE, Messages
  enabled/adaptive and private playground completed with provider usage matching
  PostgreSQL settlement. Real `max` and `xhigh` requests succeeded. `ultra`
  returned upstream HTTP 400 `invalid_value` for `reasoning.effort`; removed it
  from Codex capabilities and added a safe explicit error instead of generic 502.
- Three real 3,820-input-token cache probes returned cached=0; no cache-hit claim
  is made. Earlier interrupted requests remain pending, excluded from known
  token totals rather than silently refunded or assigned fabricated usage.

### GPT-5.6 Reserve quota and routing — 2026-09-26

- Added strict parsing and durable storage for `additional_rate_limits`, including
  the upstream `gpt-reserve` entitlement and its independent primary/secondary
  windows.
- Account cards and detail views now show Reserve quota separately, preserve
  unknown/stale state, and never render missing allowance as a full balance.
- Published Reserve metadata is Luna-derived while requests retain the literal
  `gpt-reserve` model ID. Routing requires all upstream Reserve predicates and
  fails closed on missing, stale, mismatched or errored snapshots; it never
  falls back to ordinary Luna/Astra or an account outside the configured pool.
- Added 16 PostgreSQL integration tests plus parser, routing and browser UI
  coverage for eligibility, aliases, allowlists, refresh persistence and
  pinned-session revalidation.

### GPT-6/GPT-5.6 model publication — 2026-09-26

- Published `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`,
  `gpt-5.6-luna` and `gpt-5.5`; each received 226 Codex OAuth bindings and an
  explicit `round_robin/all_accounts` pool with retry limit 7.
- Bounded live verification returned HTTP 200 and `MODEL_OK` for all seven
  enabled text models: the six above plus `gpt-6-astra`. Temporary smoke key
  and customer were revoked/disabled afterward.
- `gpt-5.3-codex` remains disabled because the upstream explicitly rejected it;
  reference catalog presence is not treated as account entitlement.

### Full Codex account-pool rotation — 2026-09-26

- Removed the diagnostic single-account routing behavior. Admin can explicitly
  enable `all_accounts` so newly linked eligible OAuth credentials join the
  model pool without editing the pool every time.
- The catalog cursor rotates the starting account between requests. An unpinned
  Codex request can try up to eight distinct accounts, matching the Cockpit
  reference bound; sessions and continuations remain pinned to one account.
- Only pre-generation `401`, `429`, model-entitlement rejections and fenced
  refresh failures move to another account. Ambiguous 5xx/transport/started
  streams are never replayed. Per-account proxy selection and one-time quota
  settlement remain attached to each attempt.
- Added binding/model cooldown persistence and full-pool rotation tests.
- Local pool switched by versioned admin API to `round_robin`, `all_accounts=true`,
  `retry_limit=7`; diagnostic single-account pin removed. Four live requests
  completed on four distinct accounts (attempt counts 1/3/1/2). Exact ledger
  settlement: 19/20/20/20 weighted tokens, once per request. Temporary keys
  revoked and test customers disabled. 226 bindings are configured; only 51
  accounts were active, 155 unverified and 20 refresh-uncertain at verification.

### Live Codex verification and stream repair — 2026-09-26

- Fixed a real false-502 failure: Codex can finish a complete item lifecycle
  with `response.completed.output=[]`. Codex HTTP/WS parsers now retain the
  streamed items and provider usage; generic Responses parsing stays strict.
  Unclosed items, missing terminal events and conflicting nonempty output are
  still rejected. No usage is invented when a stream fails.
- Added safe `model_not_supported` / `provider_rejected` classification for
  Codex HTTP 400 details without returning provider bodies or retrying other
  accounts. Added Codex runtime Originator/User-Agent headers based on the
  reference contract; these do not grant model access.
- Live public `/v1/responses` verified over direct egress with one imported OAuth account and
  `gpt-6-astra`: HTTP 200, `completed`, `OK`, input 10 / cached 0 / output 5.
  Database confirms one completed serving attempt and 15,000,000 micro-units
  settled (15 weighted tokens at factor 1). Temporary test keys were revoked.
  Two earlier parser-failed diagnostic requests remain usage-pending because
  their provider usage was not retained; no fabricated refund or settlement.
- Local test setup publishes `gpt-6-astra` with an explicit dynamic all-account
  pool; `gpt-5.3-codex` remains disabled after an explicit upstream model
  rejection. This is not proof of entitlement for all 226 imported accounts.
- Started the local deployment under a transient systemd user service
  (`autobuild-local-gateway`) so it survives terminal closure; documented
  graceful stop/restart. This does not enable startup after reboot.
- Verification: 57 targeted Python tests passed, including synthetic HTTP
  JSON/SSE, Chat compatibility, cached usage, rejection release/no-retry,
  incomplete lifecycle and existing reasoning/generation-control regressions.
  Final expanded runs: **1,466 gateway + 167 remaining Python tests passed**
  (1,633 total; two dependency deprecation warnings), **49 Node UI tests**,
  Ruff, compileall and diff-check passed. Synthetic WebSocket coverage includes
  empty-terminal sequential turns with exact cached-token settlement.

### Codex service UI rebuild — 2026-09-26

- Added a durable server-side **Refresh quota all** job: full-account scope
  independent of pagination/search, bounded concurrency, progress/error/skip
  counts, explicit stop, idempotent request IDs, rate-limit stop, and recovery
  fencing after worker interruption. Added opt-in automatic refresh in minutes
  (off/2/5/10/15/custom 1–999), CAS-protected settings, PostgreSQL persistence,
  and backup/restore handling that disables schedules after restore.
- Added real server-side numbered pagination for OAuth accounts (`page`, `limit`,
  `q`, `status`), with totals and bounded page controls (24/48/96 rows), direct
  jump/first/last/previous/next navigation, and page-scoped snapshot reads. The
  legacy cursor response remains supported for existing clients.
- Replaced the accumulated dark/light CSS with a compact light admin surface.
  Implemented the hierarchy from the local Cockpit reference's
  `CodexApiServiceView.tsx` and `CodexApiServicePage.css`: Accounts title,
  group switcher/floating tabs, service strip, and tab-specific panels.
  This is independent DOM/CSS code, not a copied React/Tauri application.
- Account cards retain full email and stored quota/reset-credit information.
  Routing is the real versioned pool editor beside the cards, not cosmetic
  settings. Batch OAuth import is now a dialog preserving drafts and FileList.
- Client-key cards have compact quota summaries and expandable policy details.
  Added in-tab key creation/editing/toggling/rotation/revocation and one-time
  secret dialogs; quota grants retain existing exact arithmetic and CAS checks.
- Models use a selectable catalog and separate capability/rule panels. Reports
  include range presets, exact BigInt page summaries and an attempt trend chart.
  Paginated totals are labelled as page-scoped; missing usage remains unknown.
- Verification: **49 Node unit tests**, six PostgreSQL job tests (including a
  1,000-account two-worker run), 12 account-admin tests, Codex browser/regression
  suite (including
  226-record batch import), gateway CRUD smoke and new five-tab desktop/mobile
  layout/key-dialog smoke passed against temporary PostgreSQL. Some repeated
  regression runs hit asynchronous Playwright route/element teardown races;
  final runs passed. No live OAuth, reset, Kiot rotation or inference was run.
- Not a claim of full Cockpit parity: no desktop Sidecar lifecycle controls,
  fabricated service liveness, unsupported routing fields or estimated prices.
  Backend transport limitations documented below remain unchanged.

### Codex transport parity — 2026-09-26

- Hardened WebSocket handshake validation, safe per-turn error/stream-ID correlation,
  duplicate Origin rejection, idle/deadline closure and cancellation-safe cleanup.
  Added RFC6455 and real httpcore CONNECT/upgrade tests through direct/fixed/pool/
  synthetic Kiot egress, with PostgreSQL cached-token settlement and disconnect holds.
- Verification: full Python suite **1,612 passed** (Python 3.14, temporary
  PostgreSQL 18; two dependency deprecation warnings), **43 Node tests**, Ruff,
  compileall/diff check and Codex browser smoke passed. The browser run had one
  transient fixture socket hangup; rerun passed. No live provider inference used.
- Documented current sequential WebSocket/warmup/connection-cache limitations;
  these tests do not certify full Cockpit or Codex CLI parity.
- Added a local Codex model catalog and `/v1/models` projection, with admin
  capability metadata for compact, image generation/edit and Responses WebSocket.
- Added `/v1/responses/compact`, OpenAI-compatible `/v1/images/generations` and
  multipart `/v1/images/edits`, plus the Responses WebSocket beta bridge with
  configured direct/proxy egress and `responses_websockets=2026-02-06`.
- Added image inputs, image-generation calls, compaction items and partial image
  events to the internal Responses contract; quota settlement remains on the
  existing input/cache/output accounting path.
- Removed non-Codex product labels from the admin shell; Codex model, binding,
  proxy, OAuth, key and usage controls remain available on mobile navigation.

### Codex parity and proxy follow-up — 2026-09-26

- Added private Codex service proxy selector with direct/fixed/list/pool/KiotProxy
  profiles, CAS-safe provider configuration, encrypted-profile redaction and strict
  precedence `credential → provider → direct`. Configured proxy failures fail closed;
  customer responses never contain proxy fingerprints or credentials.
- Codex Responses normalization now strips unsupported sampling/output-cap hints while
  retaining certified quota bounds, adds SSE `Accept`, and preserves exact cached/
  reasoning/output settlement. Native Responses reasoning summaries and opaque
  encrypted replay survive JSON/SSE; incompatible protocols reject replay or omit
  unsupported output without leaking raw reasoning.
- Added persistent loopback development runner `scripts/local_gateway.py`, which
  initializes a private PostgreSQL/keyring directory and can batch-import the existing
  local OAuth JSON without re-authenticating accounts. It never prints tokens.
- Verification: 1,393 gateway Python tests, 43 Node UI tests, gateway browser smoke,
  Codex browser smoke (real PostgreSQL/session, proxy CAS and 226-row import), Ruff and
  diff checks passed. Live Codex/Kiot/provider credentials were not used.

### Final review follow-up — 2026-09-25

- Credit reset bị khóa khi lần refresh credits gần nhất có lỗi; snapshot cũ không
  được dùng để gửi POST consume. Bổ sung kiểm thử preflight, prepare và dispatch.
- Account đã biết hết quota không tự quay lại pool chỉ vì snapshot quá hạn hoặc
  refresh lỗi; chỉ quan sát upstream mới, hợp lệ mới xóa trạng thái exhausted.
- Review độc lập xác nhận cả hai lỗi đã được sửa; 185 test tập trung qua.
  Full Python3.14 trên bản sửa: 1445 passed, hai warning dependency. Chi tiết tại
  `docs/codex-final-review.md`; Python3.10 hiện không có trong checkout này.

### Codex API Service handoff — 2026-09-25

- Hoàn tất reset recovery: HTTP 4xx từ chối consume chỉ bỏ khóa UI sau khi đọc lại
  credits/operation thành công, không tự gửi lại. Explicit admin refresh về sau có
  đủ usage + credits fresh sau receipt có thể hoàn tất `succeeded_refresh_failed`
  qua fence generation/status/stamp sẵn có. Partial/stale refresh vẫn khóa;
  `unknown` vẫn cần evidence resolution. UI nhận terminal evidence của server,
  không suy ra thành công từ phần trăm hoặc `active_reset=null`.

- Thêm e2e synthetic composition của identity, customer ledger, PostgreSQL, proxy
  manager, Codex pool/engine và fake HTTP: key 100m → 429 → SSE cached usage →
  per-account reports → admin quota refresh/reset; unknown reset không tự retry.
- Codex Accounts UI v6 đã được duyệt và hiện diện trong admin: masked account state,
  exact decimal quota/grant, actual window duration, explicit reset/evidence resolution.
  Provider Catalog UI vẫn pending, không nằm trong handoff này.
- Ghi rõ ba quota tiers độc lập (customer weighted-token, upstream windows, reset
  credits), proxy precedence credential → provider → direct và private routes.
  Tại checkpoint này compact/images/WebSocket/profile takeover chưa được hỗ trợ;
  không hứa full CLI parity.
- Sửa `admin/usage.py`: year-0001/default-window và UTC offset overflow trả safe 422
  thay vì 500, có authenticated regressions.

Các số liệu checkpoint bên dưới là lịch sử, không thay thế final verification Task12.
Task12: 1312 Python tests trên mỗi runtime3.10/3.14 (một Google GenAI warning3.14),
34 Node tests, ba browser smokes, Ruff cả hai, compileall/diff-check qua. Isolated
build bị chặn vì thiếu ensurepip3.14; no-isolation sdist/wheel qua. Không live provider,
deployment hay independent whole-branch approval trong task này.

### Added

- Gateway FastAPI riêng cho OpenAI Chat/Responses, Anthropic Messages, Gemini
  native và Ollama JSON/streaming.
- UI quản trị tiếng Việt `/service/` theo Superdesign v3: khách/key/policy,
  provider/credential, model/alias/mapping, proxy, OAuth, usage/audit, budget,
  playground JSON.
- Client key hiển thị một lần, digest lưu trữ, rotation/revoke/expiry, quyền
  model/giao thức và versioned updates.
- PostgreSQL quota reservation/settlement, tổng/ngày/tháng, hệ số fixed-point,
  client/provider/credential RPM và concurrency.
- Provider Base URL/auth tùy chỉnh, discovery có bước chọn, routing/cooldown,
  budget mua vào riêng; credential mã hóa AEAD record-bound.
- Direct/fixed/list proxy và KiotProxy key-per-line với lease/fencing, cooldown,
  TTL, cancellation, release explicit.
- Codex OAuth import/refresh single-flight; continuation handle mã hóa và scope
  theo key/model/account; không tự lấy account cũ phục vụ traffic khách.
- CLI init-keyring/migrate/serve/admin/maintenance/backup/restore; snapshot mã
  hóa và empty-target restore; container template và GitHub Actions.
- Kiểm thử SDK HTTP, 40 cross-protocol text/stream combinations và hai Chrome E2E.

### Fixed

- Rate limit: thống nhất HTTP 429 và `Retry-After` (số giây/HTTP-date, tối đa 24h)
  giữa 5 adapter generation và native Anthropic/Gemini token counting; không
  truyền raw provider body/header ra client.
- Chuỗi dự phòng trả hint sớm nhất chỉ khi mọi provider ứng viên đều đã bị
  rate-limit và có hint hợp lệ; không lấy bừa giá trị của provider cuối. Giữ đúng
  `Retry-After: 0`, không biến thành cooldown 60 giây.
- PostgreSQL cooldown cập nhật tăng đơn điệu giữa các worker; chặn route snapshot
  đã cũ trước dispatch. Toàn bộ tuyến hợp lệ đang cooldown trả 429 với lịch chờ
  cục bộ, không gửi thêm upstream. Không thay đổi giới hạn tối đa hai attempt.
- Token-count rejection được đánh dấu rejected và hoàn hold thay vì để usage
  pending; quota/budget/proxy vẫn cleanup theo lifecycle hiện có.
- Tính cooldown từ lúc nhận rejection, không cộng lại thời gian cleanup; hint
  cuối phản ánh tuyến đã cooldown và cập nhật dài hơn từ worker khác. Responses
  continuation dùng cooldown binding gốc, không bị đổi thành `invalid_state` chỉ
  vì đang rate-limit; handle không hợp lệ vẫn bị từ chối.
- GitHub Actions: quote healthcheck PostgreSQL đúng cho Docker arguments; hai
  Python matrix jobs chạy độc lập để giữ đầy đủ kết quả khi một job lỗi.
- Browser test gateway tôn trọng `AUTOBUILD_TEST_POSTGRES_BIN` và database URL
  test-only như bộ pytest, không bắt buộc binary nằm trong thư mục checkout.
- CI Python 3.10 phát hiện race cleanup sau disconnect: đóng request single-flight,
  chờ accounting/lease hoàn tất và đóng async generator rõ ràng trước khi teardown.
- Adapter/egress preflight trước dispatch, không giữ quota cho request chưa gửi.
- Recheck OAuth health dưới refresh lease, không replay grant không rõ kết quả.
- Chờ blocking HTTP kết thúc trước khi giải phóng proxy lúc cancellation.
- Heartbeat legacy OAuth hủy đúng task xử lý; giữ initial Anthropic tool arguments.
- Gắn money hold với request để phục hồi crash trước dispatch, kể cả khi customer
  quota đã được hoàn trước một lần restart khác.
- Theo dõi disconnect trong prepare/collect/stream, không chỉ SSE.
- Quarantine tuyến vượt input/output bound, kể cả khi hệ số phí bằng 0.
- Honor upstream auth mode; giới hạn provider dùng chung giữa các client key.
- Quota lớn không bị JS làm tròn; stale response không tái hiện data sau logout;
  secret không lưu trong browser storage.

### Changed

- Ghép gateway vào codebase chính; giữ OAuth-only và schema export cũ.
- OAuth Workbench thêm chọn proxy/Kiot, giữ legacy default.
- README cập nhật onboarding; hướng dẫn OAuth chi tiết chuyển tới
  `docs/oauth-workbench.md`; thêm changelog và kiểm tra lịch sử trước private push.

### Verification / limitations

- Rate-limit follow-up: **549 tests** qua trên Python 3.10/3.14 (thêm 99 trường
  hợp), 12 Node tests, hai Chrome E2E, Ruff/compile/build qua. Review độc lập và
  focused re-review không còn Critical/Important. Còn một edge Minor về mã lỗi
  của continuation cũ khi admin đổi credential của binding đang cooldown; không
  dispatch sang credential mới. Một warning Google GenAI vẫn còn trên 3.14.
- Local checkpoint: 446 Python, 12 Node, hai Chrome E2E, lint/compile/build qua;
  một Google GenAI deprecation warning trên Python 3.14.
- Sau private publish: local suite tăng thành 449 tests, gồm ba regression cho
  cấu hình PostgreSQL của browser test; Chrome gateway và build vẫn qua.
- Bản sửa cleanup tiếp theo: 450 tests qua trên Python 3.10 và 3.14, 12 Node,
  hai Chrome E2E và build qua. Regression đảm bảo mọi concurrent close chờ cùng
  accounting/lease cleanup; không báo closed trước khi thực sự hoàn tất.
- Review độc lập có 10 Important findings; implementer tái hiện và sửa bằng
  regression tests. Không tuyên bố reviewer đã duyệt lại sau fix.
- Gateway chưa live-verify provider/Kiot/Codex inference bằng key thật; chưa
  public deployment, payment/media APIs hay tương thích đầy đủ mọi client.
- Quyền phân phối/resale upstream phải kiểm tra riêng.

## [0.1.0] — 2026-09-23

### Added

- OAuth HTTP workbench: `Email|Password|2FA Secret`, lọc dòng lỗi, proxy parser,
  batch tuần tự/song song, worker giới hạn và cancellation.
- PKCE/state, desktop-auth link, callback một lần, ID-token validation và JSON export.
- FastAPI localhost với session/CSRF, UI tiếng Việt assets local, lịch sử batch,
  nhóm output success/errors/phone_verify/filtered/cancelled.
- Kết quả bền vững, quyền file riêng tư và recovery interrupted/storage failure.

### Fixed

- Bootstrap providers → CSRF → sign-in với UUID/cookie mới mỗi attempt.
- Không đánh nhầm Sentinel metadata đơn thuần thành CAPTCHA.
- Diagnostics đã lọc bí mật phân biệt timeout/HTTP/proxy/workspace/email verify;
  giữ workspace được chọn và chi tiết lỗi qua UI/export.

### Verification

- Một account được phép đã OAuth thành công trong thử legacy; không phải tỷ lệ
  thành công toàn bộ danh sách và không phải live gateway inference test.
- Checkpoint legacy: 152 Python, 8 Node và Chrome smoke qua.
