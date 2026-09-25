# Changelog

Ghi nhận chức năng và bằng chứng kiểm thử. `Unreleased` không phải GitHub Release
hay cam kết production-ready; version package hiện vẫn là `0.1.0`.

## [Unreleased] — 2026-09-24

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
  compact/images/WebSocket/profile takeover chưa được hỗ trợ; không hứa full CLI parity.
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
