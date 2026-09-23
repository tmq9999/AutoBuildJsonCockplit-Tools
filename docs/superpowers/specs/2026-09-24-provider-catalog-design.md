# Provider & Catalog — đợt 1 của gateway mở rộng

Ngày: 2026-09-24. Trạng thái: **bản đặc tả chờ người dùng duyệt**.
Người dùng đã duyệt hướng thiết kế trong hội thoại bằng “được”; phản hồi đó
cho phép viết tài liệu này, chưa phê duyệt nội dung chi tiết hoặc kế hoạch code.

Nền kỹ thuật: commit `20a1caf` trên `feat/api-gateway`, đã mở
[PR #1](https://github.com/tmq9999/AutoBuildJsonCockplit-Tools/pull/1), chưa merge
tại thời điểm viết. Nhánh tài liệu mới: `feat/provider-catalog`, dựa trên commit
đó; không thay đổi PR #1, không tự merge/push hoặc restart dịch vụ.

## 1. Mục tiêu và tiêu chí thành công

Gateway phục vụ nhiều khách bằng API key riêng; admin có thể nối upstream của
mình, khám phá model, xem thay đổi rồi công bố những model đã chọn. Giảm nhập
tay nhưng giữ quyền quyết định model, giá, capability và quota ở admin.

Giữ codebase Python/FastAPI riêng, PostgreSQL có thẩm quyền, UI private tiếng
Việt, tất cả chế độ direct/fixed/pool/KiotProxy. Không sao chép source hay triển
khai FreeLLMAPI thành sidecar; không suy quyền resale hoặc “miễn phí” từ catalog.

Đợt này hoàn thành khi admin thực hiện được:

1. Tạo provider từ preset hoặc cấu hình custom, chọn credential/proxy rõ ràng.
2. Lấy danh sách model đầy đủ trong giới hạn phân trang; thấy metadata có nguồn
   gốc, trường chưa biết và kết quả kiểm tra kết nối không gây hiểu nhầm.
3. Xem chênh lệch với lần đồng bộ thành công trước; chọn công bố hoặc bỏ qua.
4. Công bố atomically vào model/binding hiện có, không làm rộng quyền key ngầm.
5. Bật lịch sync riêng từng nguồn; restart/nhiều worker không làm mất quyết định
   thủ công, không ghi đè kết quả mới bằng tác vụ cũ.
6. Chủ động thử inference nhỏ có cảnh báo chi phí và accounting nội bộ, không
   trừ quota của một khách bất kỳ và không tự replay khi kết quả không rõ.

## 2. Phạm vi và lựa chọn kiến trúc

Lựa chọn đã thống nhất là Provider & Catalog trước, thay vì làm API mới hoặc
cổng khách hàng trước. Hai hướng kia vẫn là đợt sau, không bị loại khỏi sản phẩm.

**Trong phạm vi:** preset, discovery/check, snapshot/diff, metadata, publish/
ignore, lịch sync opt-in, probe thủ công, quản trị và kiểm thử liên quan.

**Ngoài đợt này:** adapter inference mới; embeddings/media/vision/structured
output; routing `auto:*` hoặc chấm điểm latency; portal khách, thanh toán; feed
catalog từ xa có chữ ký; tự nhập key/account; đổi cơ chế OAuth hoặc vượt verification.
Metadata có thể mô tả một loại model chưa hỗ trợ, nhưng không tạo endpoint giả
hay binding hoạt động cho loại đó. Desktop/mobile app, fusion và compression
không phải mục tiêu của đợt Provider & Catalog.

Giữ modular monolith, không thêm Redis, broker hay microservice. Các thành phần:

| Thành phần | Trách nhiệm |
|---|---|
| `providers/presets` | Preset dữ liệu cục bộ, version và adapter/discovery mode hợp lệ |
| `providers/discovery` | Parser/pagination riêng từng wire format; không quản lý pricing của khách |
| `catalog` domain mới | Source, immutable snapshot, diff, decisions và transaction công bố |
| Operation runner | Claim job, snapshot cấu hình, lease, deadline, cancellation/recovery |
| Probe service | Một generation được admin xác nhận, accounting mua vào và evidence |
| Admin API/UI | Các lệnh private và trạng thái, không gọi HTTP upstream từ browser |
| Maintenance | Claim lịch đến hạn, đối soát operation và dọn lịch sử không còn tham chiếu |

Module `providers/catalog.py` hiện có tiếp tục chọn tuyến inference. Domain mới
không gộp networking/parser/scheduler vào file đó; publisher gọi/reuse validator
model/binding bằng cùng một DB transaction, không xâu nhiều CRUD commit độc lập.

```text
Provider + credential + effective proxy
    → job discovery → đủ trang, đủ kiểm tra → snapshot bất biến
    → diff + quyết định admin → transaction publish → public model/binding

Lịch sync → chỉ job discovery → không tự publish/enable/đổi giá
Probe thủ công → binding đã cấu hình → giữ budget → một attempt → quyết toán
```

## 3. Preset, source và identity

### Preset ban đầu

Preset bao phủ các adapter đang có: custom OpenAI-compatible Chat, custom
OpenAI-compatible Responses, Anthropic native, Gemini native và Ollama native.
Mỗi preset có ID/version, mô tả, adapter, wire API, auth mode và discovery mode;
API root do admin xác nhận. Không nhúng catalog model, mức giá, quota miễn phí,
credential hoặc khẳng định model nào đang hoạt động. Tạo bằng preset không phát
sinh request mạng; cập nhật preset trong release không sửa provider đã lưu.

Codex OAuth vẫn dùng màn hình/import/binding hiện tại. Không giả dùng `/models`
của Platform với subscription token; discovery và probe nội bộ mới trả
`unsupported_feature` cho adapter này trong đợt 1. Playground hiện có giữ nguyên.

### Source

Một discovery source chọn **một provider ID và một credential ID** thuộc provider
đó. Unique theo cặp ID, có `enabled`, `version`, discovery mode, tùy chọn lịch, `next_run_at`
và `latest_successful_run_id`. Chọn credential rõ ràng vì hai key cùng provider
có thể thấy catalog khác nhau. Không hợp nhất source dựa trên tên hiển thị,
hostname hoặc tên model; provider UUID đã có là ranh giới cấu hình upstream.

Auth `none` vẫn dùng bản ghi credential được chọn để giữ identity/admission của
schema hiện tại, nhưng không đọc/gửi secret trong request no-auth. Không tự mở
allowlist private network hoặc nới HTTPS để preset Ollama chạy được.

Effective proxy: override trên credential nếu có → profile trên provider →
direct. Snapshot gồm ID/version provider, credential, profile, source; secret chỉ
giải mã vào RAM lúc dùng. Nhiều trang của một job giữ cùng credential, root và
proxy lease; không đổi host/key giữa chừng. Mọi request vẫn qua egress guard.

Sửa root, auth, adapter, wire API, credential secret hoặc effective proxy làm
snapshot cũ **stale**, cần discover lại trước khi publish. Đổi lịch đơn thuần
không đổi nội dung snapshot nhưng vẫn dùng optimistic version cho cập nhật.
Content/config fingerprint bỏ qua lịch và label hiển thị; version optimistic
vẫn phản ánh mọi edit. Publish gửi version source hiện tại, đồng thời so fingerprint
nội dung snapshot với cấu hình hiện tại, không vô hiệu catalog chỉ vì đổi lịch.
Tắt provider/credential chặn operation mới; đang chạy được hủy có giới hạn và
không được publish kết quả. Endpoint identity không bị suy diễn bằng tên model.

## 4. Discovery và dữ liệu không tin cậy

### Hợp đồng parser

| Mode | Request cố định | Envelope và phân trang |
|---|---|---|
| OpenAI-compatible single-page | GET `models` | `data[]`, mỗi entry có `id`; không giả có pagination chuẩn |
| OpenAI-compatible cursor, opt-in | GET `models` | `data[]`, `has_more`, `last_id`; query `after`, `limit` |
| Anthropic native | GET `models` | `data[]`, `has_more`, `last_id`; query `after_id`, `limit` |
| Gemini native | GET `models` | `models[]`, `name`, `nextPageToken`; query `pageToken`, `pageSize` |
| Ollama native | GET `tags` | `models[]`, `name` hoặc `model`; một trang |

Cursor mode chỉ bật khi upstream công bố contract đó; không tự đoán chế độ bằng
brand/domain. OpenAI single-page gặp tín hiệu rõ còn trang phải báo
`catalog_incomplete`, không coi phần đầu là snapshot đầy đủ. Một list rỗng hợp
lệ khác với envelope lỗi. Không theo `next` URL, HTTP redirect, Link header hoặc
URL trong JSON. Cursor chỉ là giá trị opaque được URL-encode cho suffix cố định.

Mở rộng allowlist `OutboundRequest` bằng request kind discovery và các cặp query
đúng mode; không cho client inference dùng arbitrary query hoặc URL. Các query
auth/key/token không được thêm. `alt=sse` hiện có vẫn chỉ thuộc Gemini inference.

Giới hạn bắt buộc, không override bằng dữ liệu upstream:

- Tối đa 10 trang, 10.000 ID duy nhất, 2 MiB JSON/trang và 8 MiB toàn operation.
- Deadline tổng `min(provider.timeout, 60 giây)`, tính cả đợi proxy và các trang;
  mỗi HTTP call dùng thời gian còn lại, không bắt đầu lại timeout ở trang sau.
- Cursor tối đa 2.048 ký tự, không control character; cursor lặp hoặc `has_more`
  thiếu cursor hợp lệ là lỗi. Không lưu/log cursor hay raw request URL có query.
- ID upstream 1–200 ký tự, không control characters. ID public dùng validator
  `ModelId` hiện có; ID không đạt chuẩn public vẫn cần admin chọn mapping hợp lệ.
- Entry trùng ID và metadata giống nhau được dedup; metadata mâu thuẫn hoặc row
  lỗi làm fail snapshot. Không drop row rồi suy ra model đã bị upstream xóa.

Thiếu/sai schema, vượt cap, timeout trang sau hoặc 429: operation không có snapshot
hoàn chỉnh, không cập nhật `latest_successful_run_id`, không sinh removal diff.
Giữ bản thành công cũ. HTTP 429 giữ Retry-After và cooldown dùng chung đã có;
không đổi proxy/credential để tiếp tục, không retry trong cùng operation.

### Metadata và provenance

Mỗi entry chỉ lưu các trường được cho phép: upstream ID, display name/owner có
giới hạn, created/shutdown date, modality, input/output/context limit được công
bố, capability được công bố, thông tin giá với currency/unit tường minh. Mỗi
trường có `source=upstream`, source field path, observed timestamp và nullable
value; unsupported/unknown phải phân biệt. Raw body và raw error không lưu.

- `/models` chuẩn OpenAI cung cấp metadata cơ bản; không mặc định có token limit,
  capability hay pricing. Model xuất hiện không chứng minh inference permission.
- Không suy tools/vision hoặc family identity từ tên; tên có `free` không chứng
  minh miễn phí. Không suy đơn vị giá bằng độ lớn con số. Giá thiếu unit/currency
  vẫn unknown, không được tự đưa vào cost schedule.
- Metadata discovery không trở thành bound đã chứng nhận, giá bán, hệ số quota
  hoặc capability hoạt động. Admin xác nhận cấu hình publish riêng.
- Payload phản chiếu secret đã biết phải bị loại trước persistence/output;
  trường text render bằng text nodes, không HTML/Markdown upstream. Parser không
  được biến dữ liệu tùy ý thành log exception. Không hứa phát hiện mọi secret lạ
  do upstream tự nhét vào metadata; metadata chỉ ở private admin đến lúc duyệt.

## 5. Snapshot, diff và quyết định admin

Snapshot là toàn bộ normalized entries của một lần discovery hoàn chỉnh, gắn
source/config versions. Chỉ commit entries và latest pointer khi tất cả trang
thành công, lease/fence vẫn thuộc job và config chưa đổi. Không giữ transaction
DB mở trong lúc HTTP. Snapshot không bị sửa sau commit.

Diff so latest complete snapshot với complete snapshot liền trước cùng source:
`added`, `changed`, `missing`, `unchanged`. Lần đầu tất cả là added; model missing
chỉ là quan sát, không auto-disable/delete binding đang phục vụ khách. Lỗi sync
không bằng catalog rỗng. Diff không gộp cùng ID qua hai credential/provider.
Content digest không gồm run ID, observed timestamp hoặc thứ tự entry; chỉ đổi
metadata có ý nghĩa mới tạo `changed`. Đổi root/credential/proxy làm baseline cũ
khác fingerprint: UI báo nguồn đã đổi và không suy missing giữa hai môi trường.

Quyết định admin lưu theo `(source_id, upstream_model_id)`:

- **ignore:** giữ qua sync/restart; model biến mất rồi xuất hiện vẫn ignored.
  Undo ignore là mutation có version và audit.
- **published:** lưu binding ID và entry digest đã duyệt. Metadata đổi được đánh
  dấu cần review, không tự ghi lại binding/model. Binding bị admin tắt vẫn tắt.
- Quyết định luôn explicit; nguồn sync không tự sửa các decision hoặc key policy.

### Publish

Một lệnh tối đa 100 lựa chọn, có snapshot/run ID, expected source/config versions,
expected decision/binding/public-model versions và idempotency operation ID.
Snapshot phải là latest complete, không stale, chưa quá 24 giờ; nếu không phải
discover lại. Public ID, model identity, input/output bounds, capability, trạng
thái enabled và hệ số token phải được admin xác nhận, không lấy ngầm từ preset.

Chỉ nhận text/basic tools/continuation mà adapter đang hỗ trợ. Metadata về media,
embeddings hoặc vision không cho phép publish chức năng chưa có codec/test.
Không tự tạo alias hoặc router model để ghép hai model khác nhau. Trường tools
mặc định chưa chọn, không kế thừa mặc định `text,tools` của form binding cũ.

- Với model public mới: tạo model + binding + decision + audit trong **một**
  transaction. Mặc định draft/disabled; bật phục vụ là lựa chọn hiển thị rõ.
- Gắn vào model public đã có: model identity phải khớp, chỉ thêm binding; không
  đổi hệ số/giá/model enable. Xung đột hoặc model ID đã tồn tại trái dự kiến →
  409, rollback toàn bộ batch, không commit một phần.
- Muốn đổi binding đã publish: cung cấp expected version và diff admin đã chọn;
  sync không có quyền tự thực hiện thao tác này. Callback/job chạy chậm không
  ghi đè chỉnh sửa mới hơn.
- Retry cùng operation ID và cùng payload trả kết quả đã commit, không nhân đôi
  binding/audit; ID giống nhưng payload khác → 409. Không tái sử dụng claim này
  cho inference probe.

Publish không thêm model vào allowlist key. Key đã có `all_models=true` sẽ thấy
model mới khi được enabled theo đúng policy hiện có; UI phải cảnh báo điều này
trước khi enable. Với key allowlist cụ thể, admin phải cấp quyền riêng.

## 6. Kiểm tra kết nối và inference probe

### Check mặc định: không generation

Chọn source và chạy một GET list đầu tiên, parse response tối thiểu, không ghi
snapshot catalog hay thay quyết định. Trả `reachable`, `catalog_readable`, status
code an toàn, duration và thời điểm; authentication luôn `unverified` nếu chỉ
có response 200. HTTP 401 chứng minh request bị từ chối, không khẳng định nguyên
nhân vĩnh viễn; HTTP 403 có thể là policy/tier, không tự gắn key là sai.

Check và discovery không tự đổi `credentials.health` hoặc làm OAuth credential
unverified thành active, không xóa cooldown theo Retry-After. Chúng tiêu RPM và
slot provider/credential, nhưng không giữ/trừ generation quota khách.

### Probe inference: explicit và có budget

Nút riêng “Thử inference — có thể tính phí”, không gộp vào check, publish, startup
hay scheduler. Chỉ chạy trên binding text đã được admin cấu hình với đúng source
credential; không tự chọn model đầu tiên vừa discover. Một request dùng prompt
cố định `Reply with OK.`, output cap 16 tokens; không tools, continuation, dữ liệu
khách, browser, shell hoặc vòng thử đến thành công. Output chỉ được kiểm tra
không rỗng rồi bỏ; không lưu prompt/response trong history.

Điều kiện trước dispatch: adapter hỗ trợ output cap, finite verified input bound,
provider cost schedule tường minh và budget cùng currency; thiếu thì trả
`probe_budget_required`/`unsupported_feature`, không tự giả giá bằng 0. Giá 0
chỉ hợp lệ nếu operator đã cấu hình rõ. Codex OAuth excluded như mục 3.
Binding phải có output bound ít nhất 16; deadline probe tổng tối đa
`min(provider.timeout, 60 giây)`. Request xác nhận ghi expected binding/provider/
credential/proxy/budget versions và mức giữ tối đa; cấu hình đổi lúc chờ queue
phải stale, không tự chạy với khoản chi mới mà admin chưa xác nhận.

Probe có operation UUID, payload digest, attempt ID, config/cost snapshot, usage
và monetary settlement riêng. Reuse adapters/events/usage normalization,
`ProviderLimits`, `ProxyManager` và `BudgetService`; **không** mượn customer key,
không ghi customer request/quota ledger và không dùng đường playground khách.
Giữ budget theo input bound và output 16 trước HTTP. Mỗi operation ID chỉ một
generation attempt; retry POST tạo job trả job cũ, không phát sinh attempt thứ hai.

- Thành công và đủ usage → settle actual mua vào, trả `inference_verified` tại
  thời điểm đo, không hứa toàn bộ capability/tier hoặc tự bật route.
- Rejection trước generation → release money hold, sanitized error/cooldown.
- Timeout/disconnect sau dispatch, thiếu usage hoặc worker crash → `usage_pending`,
  giữ hold và không auto replay. Recovery dùng evidence đã persist để settle một
  lần; không có evidence thì cần admin đối soát.
- Usage vượt bound → giữ evidence, tính chi phí theo contract hiện có, quarantine
  binding và báo incident; không giả usage nhỏ để khớp hold.
- Private reconciliation cho probe pending: admin xác nhận actual cost/currency
  và reason/evidence; transaction idempotent, audit `admin_adjusted`, không sửa raw
  usage gốc. Không giải phóng unknown hold bằng thao tác hủy/xóa lịch sử.

UI hiển thị internal probe usage/cost riêng, không gộp vào usage của khách. Nếu
operator cần thử custom prompt/giao thức, playground cũ vẫn có contract dùng
client key và trừ quota khách như trước.

## 7. Operation runner, đồng bộ và recovery

Manual actions tạo job durable và trả 202. Maintenance process hiện có được
bổ sung runner được inject cùng vault/transport/proxy services; public listener
không chạy scheduler riêng. Admin khởi tạo job kể cả khi worker offline và UI
báo queued, không tạo coroutine fire-and-forget thay cho durable runner.
Maintenance recovery tick hiện có không được chờ toàn operation 60 giây: runner
dùng task group có giới hạn hai job hoạt động mỗi process, lease riêng theo source
và heartbeat, không giữ lease maintenance 30 giây để bao networking dài hơn nó.
Shutdown chờ cleanup có deadline; nhiều process vẫn dựa vào PG admission/lease.

Mỗi source chỉ có một job queued/running/cancel-requested cùng lúc; PG constraint
và fencing lease bảo đảm giữa nhiều process. Lệnh đồng thời cùng loại và payload
trả existing job; khác payload trả conflict. Scheduler skip source đang bận.
Check/discovery/probe đều snapshot version trước chạy; claim/generation hợp lệ
mới được commit. Không chỉ dựa vào timestamp hay khóa RAM.
Operation được chấp nhận không phụ thuộc kết nối browser: ngắt poll/đóng tab
không hủy generation; cancel explicit mới yêu cầu dừng. `usage_pending` sau probe
không chặn discovery/check của source nhưng vẫn giữ budget để tránh chi quá mức.

Giữ một proxy lease cho cả operation; từng GET page xin admission RPM/concurrency
riêng, tránh tính 10 trang là một request. Probe một admission cho attempt duy nhất.
Không giữ row lock qua HTTP; khi admission fail/rate-limit thì kết thúc job,
giải phóng proxy, không cố chờ cả cooldown. Heartbeat mất ownership hủy HTTP task;
stale worker không thể commit snapshot hoặc settle lần hai.
Recheck source/config trước từng dispatch và lúc commit snapshot. Disable sau
admission không thể thu hồi byte đã gửi; hủy bounded, không gửi trang kế tiếp và
không công bố snapshot, tương tự giới hạn revoke request đang chạy của gateway.

States: `queued → running → succeeded | failed | cancelled | stale`; riêng probe
sau dispatch không rõ kết quả → `usage_pending`. `cancel_requested` là flag; chỉ
terminal cancelled khi networking và lease cleanup đã xong. Pending probe không
bao giờ được claim lại như queued job.

Lịch chỉ áp dụng discovery, mặc định tắt. Khi bật, default 24 giờ, range 5 phút–
7 ngày; UTC `next_run_at` được claim atomically. Mỗi source thêm jitter 0–60 giây.
Không chạy bù mọi lần bị lỡ khi offline. Failure không tạo retry loop: lần tiếp
theo không sớm hơn chu kỳ, deadline cooldown hoặc backoff lỗi, lấy thời điểm
muộn nhất. Backoff tuần tự 5/15/60 phút cho các lỗi liên tiếp, reset khi successful;
rate limit bắt buộc tôn trọng cả Retry-After. Admin có thể dừng lịch bất cứ lúc nào.

Crash: trước dispatch thì release unused hold; đã dispatch probe thì pending;
discovery/check bỏ partial result và đánh interrupted, không publish. Chờ hard
deadline + grace của lease trước reclaim; không tái dùng slot để đổi IP trong khi
worker cũ còn quyền. Job queued quá 10 phút hết hạn, không bất ngờ gọi probe khi
operator quay lại lâu sau đó. Nhấn chạy lại cần operation ID mới và xác nhận phí.

## 8. Dữ liệu bền vững và migration

Tên bảng dùng dưới đây là contract miền dữ liệu; mapping column/index đầy đủ
sẽ nằm trong kế hoạch được duyệt sau spec.

| Bảng | Nội dung chính / invariant |
|---|---|
| `catalog_sources` | provider/credential unique, mode/version, schedule, latest complete run |
| `provider_operations` | kind, state, owner/fence, deadline/cancel, config versions, safe result/error, probe hold/evidence, idempotency |
| `catalog_entries` | `(run_id, upstream_id)` unique, normalized metadata/provenance bất biến |
| `catalog_decisions` | `(source_id, upstream_id)` unique, ignore/published, expected version, binding/digest đã duyệt |
| `catalog_publications` | unique operation ID, request digest và result mapping cho replay publish |

Source/latest run FK xử lý bằng insert run rồi update pointer trong cùng transaction;
không tạo circular insert bắt buộc. Operation probe liên kết attempt UUID của
`upstream_reservations` hiện có qua field có index; không giả customer `request_id`.
Runner recovery chịu trách nhiệm các hold này, không để maintenance cũ bỏ quên.
Reconcile/settle đọc row lock và ghi audit một lần. Delete provider/source là
disable/retire, không cascade xóa pending accounting hoặc decisions.

Migration additive sau `0009_provider_admission`, không tự bật source/lịch,
không đổi dữ liệu giá/policy/binding cũ. `discovered_models` cũ thiếu credential
và provenance nên giữ dưới nhãn legacy, không tự gán cho key đầu tiên hoặc coi
là snapshot verified/publishable. Một lần discovery mới mới tạo snapshot đầy đủ.

Lưu metadata snapshots 30 ngày, tối đa 20 complete snapshots/source, nhưng giữ
latest, previous và mọi run còn được decision/publication tham chiếu. Nếu các
tham chiếu vượt giới hạn thì giữ và báo counts, không xóa phá tham chiếu. Terminal
check/discovery history 30 ngày; probe pending/settled accounting và audit không
auto-delete trong đợt này. Chạy cleanup theo batch, tránh transaction vô hạn.

Update backup table allowlist, restore dependency order và encrypted-field check
khi thêm schema. Giữ giới hạn snapshot 64 MiB, fail rõ khi vượt; không âm thầm bỏ
catalog/accounting khỏi backup. Backup revision cũ restore bằng code/schema cũ
vào DB trống rồi migrate; không ép restore khác revision vào database đang chạy.

## 9. API quản trị và tương thích

Tất cả endpoint dưới `/api/service`, dùng admin session/CSRF hiện có; không mount
trên public gateway. UUID resource không thay thế authorization. Response monetary
giữ decimal-string wire hiện có; raw secret/header/cursor không xuất hiện.

| Endpoint tương đối | Contract |
|---|---|
| GET `/provider-presets` | Preset cục bộ, không networking |
| GET/POST `/catalog/sources` | List/create source, schedule disabled mặc định |
| PUT `/catalog/sources/{id}` | Update có expected version |
| POST `/catalog/sources/{id}/discover` | Idempotent enqueue manual discovery |
| POST `/catalog/sources/{id}/check` | Enqueue read-only connection check |
| POST `/catalog/sources/{id}/probe` | Enqueue explicit fixed-prompt probe với binding/version/budget/ack phí |
| GET `/provider-operations/{id}` | Trạng thái và evidence an toàn, không token/body |
| POST `/provider-operations/{id}/cancel` | Durable cancellation, không giả giải phóng pending charge |
| POST `/provider-operations/{id}/reconcile` | Chỉ probe usage_pending, cost/currency/reason và version |
| GET `/catalog/sources/{id}/entries` | Current snapshot, diff, decisions; page size 1–200, default 50 |
| PUT `/catalog/sources/{id}/decisions` | Ignore/undo có expected version, tối đa 100 items |
| POST `/catalog/sources/{id}/publish` | Atomic explicit selections, versions và operation ID |

Các mutation enqueue/publish yêu cầu operation ID do client sinh; cùng ID khác
payload trả 409 cả trước và sau completion. Source/decision CRUD dùng optimistic
version. Cursor admin được gateway ký và ràng source/run/
filter, không tái sử dụng cursor upstream. Không trả full 10.000 rows mỗi UI poll.

Giữ POST `/providers/{id}/discover` cũ với payload `{}` và success shape
`{models: [...], published: false}`. Wrapper chọn credential enabled đầu theo UUID
như trước và ghi rõ ID đã dùng, không thử key tiếp theo khi lỗi. Nó dùng cùng
discovery service, hạn 60 giây, version/fencing/proxy/cap như API mới, không tạo
pipeline networking riêng. Source tự tạo bởi wrapper có schedule disabled; job
vẫn durable. Lỗi/timeout giữ error HTTP, không giả success hay trả partial list.
UI mới dùng API async có credential selector, wrapper chỉ để tương thích.
Wrapper tạo/claim job bằng cùng runner trong request (không cần maintenance đang
chạy), còn API async mới chỉ enqueue. Cùng source đang bận trả conflict; wrapper
không lập job thứ hai. Compatibility list được chiếu từ snapshot vừa hoàn tất;
không sửa/bổ sung legacy rows thiếu provenance để giả đó là nguồn mới.

Private error envelope mở rộng safe codes theo stage, gồm `catalog_incomplete`,
`catalog_limit_exceeded`, `catalog_snapshot_stale`, `catalog_conflict`,
`probe_budget_required`; không expose exception string. `SafeAdminRoute` truyền
Retry-After đã validate khi có. Upstream 401/403 hiển thị rejection diagnostic của
upstream, không đánh mất phiên admin hoặc trả nhầm “admin chưa đăng nhập”.

Public model list/inference không đổi shape hoặc capability trong đợt này. Key
khách không đọc source/discovery/operation endpoints; việc publish mới là điểm
duy nhất đưa model vào public registry, và model phải enabled mới được liệt kê.

## 10. UI chức năng và bảo mật vận hành

Giữ navigation/layout và assets local v3 đã duyệt; không redesign toàn dashboard.
Provider editor thêm preset selector; source editor chọn credential, hiển thị
effective proxy, schedule và trạng thái worker. Catalog panel có filter diff,
unknown badges, provenance/observed time, selection, ignore/undo và preview publish.
Preview phải cho thấy public ID/identity, bounds, capability, hệ số, enabled và
tác động đến key `all_models`. Không auto-select/enable toàn catalog.

Nút check và probe tách biệt. Probe hiển thị budget/currency/upper reservation,
confirmation phí và trạng thái pending/reconcile; không lưu secret vào browser
storage. Không refresh UI bằng request cũ sau logout/đổi source. UI không tuyên bố
“key sống” chỉ vì đọc được `/models`; hiển thị đúng evidence đã quan sát.

Chi tiết visual component mới sẽ đi qua Superdesign ở bước UI sau khi spec/plan
được duyệt; không tái thiết kế màn OAuth và không tạo draft mới trong bước tài liệu.

Egress: suffix cố định, auth header allowlist, DNS/IP pinning và TLS giữ nguyên;
remote-proxy DNS vẫn cần trusted proxy/firewall. Một lease xuyên suốt pagination;
proxy lỗi không fallback direct. Kiot current/new/out và cooldown vẫn theo manager
hiện tại; auth/rate rejection không kích hoạt rotate/out. Không gọi mạng lúc đọc
preset, tải UI hay khởi động với toàn bộ lịch disabled.

Mọi publish, ignore, đổi source/lịch, probe, cancel và reconcile có audit actor,
IDs/version và safe outcome; không raw secret/root query/body. Có retention cho
operation metadata, không phụ thuộc logging driver error strings để chẩn đoán.

## 11. Kiểm thử và tiêu chí nghiệm thu

TDD cho thay đổi hành vi; test DB dùng PostgreSQL tạm và upstream/proxy synthetic.
Không sử dụng account.txt, export token hay key thật trong fixtures/CI. Bằng chứng
live chỉ thực hiện riêng khi người dùng cung cấp credential và phạm vi cho phép.

- Parser cho từng mode: empty đúng, envelope sai, null/unknown, ID trùng,
  conflict metadata, invalid date/unit, secret reflection và XSS.
- Pagination: thứ tự/cursor hợp lệ, loop, thiếu cursor, cap trang/bytes/model,
  timeout/trang lỗi, cross-origin URL/redirect không được theo, deadline tổng.
- Database: hai worker cùng claim/publish, fencing worker cũ, config version đổi
  khi job chạy, proxy override sửa, chỉ full snapshot cập nhật pointer, rollback
  toàn publish batch, retry publish idempotent, expected-version conflict.
- Governance: ignore qua sync/restart, missing không tắt route, sync không sửa
  override/giá/quota/key policy; schema cũ không tự tạo schedule hoặc verified data.
- Security: private endpoint không xuất hiện public listener; CSRF/auth, no
  query secrets, credential đúng provider, SSRF/pagination query allowlist,
  source bị disable không được admission trang mới; bytes đã gửi xử lý theo
  contract cancel/uncertain charge, không hứa thu hồi request upstream đã nhận.
- Proxy: direct/fixed/pool/Kiot, credential override priority, ổn định proxy qua
  các trang, cleanup khi 429/cancel/worker loss; không đổi IP để né giới hạn.
- Probe: chưa ack/thiếu budget/cost/bounds không gửi; đúng một attempt, customer
  ledger không thay đổi, actual cost một lần, unknown giữ hold, crash recovery,
  reconcile audited/idempotent, usage vượt bound quarantine.
- Migration/backup: upgrade từ baseline có data tổng hợp, restore cùng revision,
  đủ bảng/FK/decisions, không mất pending hold; old revision bị từ chối có hướng dẫn.
- API regression: legacy discover shape, public protocols/model permissions,
  rate-limit/continuation của PR #1, tất cả test hiện có tiếp tục qua.
- Browser E2E: preset→source→discover→diff→publish/ignore; schedule; check khác
  probe; unknown/pending/error; logout/stale-response/XSS/mobile trên layout hiện có.
- Trước bàn giao: full pytest trên 3.10/3.14, Node tests, hai Chrome suites với
  cases mới, Ruff/compileall/build, secret scan và review độc lập; cập nhật
  README/CHANGELOG/compatibility theo kết quả thực, không theo test count dự kiến.

## 12. Nguồn tham khảo và trạng thái duyệt

- [Đặc tả gateway gốc](2026-09-24-api-gateway-design.md) — các bất biến về identity,
  accounting, proxy và tách public/admin vẫn có hiệu lực.
- FreeLLMAPI, pin `1346b7d39ecbc71ac4c087fcdd36248ebdbde79b`:
  [catalog sync](https://github.com/tashfeenahmed/freellmapi/blob/1346b7d39ecbc71ac4c087fcdd36248ebdbde79b/docs/en/architecture/05-catalog-sync.md),
  [custom sync](https://github.com/tashfeenahmed/freellmapi/blob/1346b7d39ecbc71ac4c087fcdd36248ebdbde79b/server/src/services/custom-model-sync.ts),
  [giữ quyết định xóa](https://github.com/tashfeenahmed/freellmapi/blob/1346b7d39ecbc71ac4c087fcdd36248ebdbde79b/server/src/services/custom-model-tombstone.ts),
  [provider validation](https://github.com/tashfeenahmed/freellmapi/blob/1346b7d39ecbc71ac4c087fcdd36248ebdbde79b/docs/en/providers/03-adding-a-new-provider.md).
  Chỉ tham khảo hành vi; không dùng auto-register, đoán giá/model identity hoặc
  cơ chế single-user làm chính sách cho gateway nhiều khách.
- [OpenAI Docs — List models](https://developers.openai.com/api/reference/resources/models/methods/list)
  đã đọc ngày 2026-09-24: basic metadata gồm ID/created/object/owned_by/shutdown_date;
  không cung cấp contract chứng nhận pricing/capability của discovery mới.

Đã tự rà soát scope, source/snapshot/publish/probe, scheduler, concurrency và
uncertain billing; bổ sung digest không phụ thuộc thời điểm quan sát, phân biệt
version/fingerprint, legacy wrapper và giới hạn hủy request đã gửi. Không có phần
code đã được triển khai bởi tài liệu này. Sau khi người dùng duyệt **file spec**,
mới viết kế hoạch triển khai chi tiết và trình lựa chọn cách thực thi.
