# AutoBuildJsonCockplit-Tools — dịch vụ API đa chuẩn

Ngày: 2026-09-24 (tiếp nối thiết kế ngày 2026-09-23).
Trạng thái: **bản đặc tả chờ người dùng duyệt**.

Người dùng đã duyệt hướng sản phẩm, quota token quy đổi và bốn chuẩn API lõi.
Yêu cầu bổ sung mới nhất là kết nối API/provider bên ngoài để cung cấp lại cho
khách hàng. Tài liệu này hợp nhất các yêu cầu; chưa phải kế hoạch triển khai,
chưa xác nhận tính năng đã được xây dựng hoặc kiểm thử.

## 1. Mục tiêu và ranh giới

Phát triển codebase riêng trong project hiện tại, cung cấp gateway cho nhiều
khách hàng bằng API key do admin phát hành. Khách không được biết key/token của
upstream. Admin quản lý khách hàng, API key, quyền model, quota, hệ số input/output,
provider, OAuth, proxy và usage trên giao diện tiếng Việt.

Nguồn tham khảo: FreeLLMAPI về gateway/provider, Cockpit về quản lý OAuth/service,
9router về model mapping và routing. Không sao chép hay đóng gói source, UI hoặc
sidecar của các project tham khảo vào phân hệ mới. Không cam kết “miễn phí”, không
giới hạn hoặc tương thích mọi tính năng chỉ vì cung cấp cùng tên endpoint.

Tiêu chí thành công: một API key của dịch vụ gọi được các model được cấp quyền
qua bốn chuẩn lõi; request đi qua provider/proxy đã chọn; usage, quota và lỗi được
ghi nhận đúng kể cả khi song song, streaming, timeout hoặc restart.

### Phạm vi bản đầu

- Khách hàng do admin tạo; một khách có nhiều API key, không cần tự đăng ký.
- CRUD cấu hình key/model/provider/proxy; khóa, thu hồi, xoay key; audit thay đổi.
- Quota tổng/ngày/tháng, RPM, số request đồng thời và ngày hết hạn theo key.
- Hệ số token input/output theo model, cho phép ghi đè theo key và model.
- OpenAI Chat Completions/Responses, Anthropic Messages, Gemini native, Ollama.
- Upstream dùng API key hoặc Codex OAuth; adapter hai chiều viết riêng.
- Provider bên ngoài có Base URL tùy chỉnh, nhiều credential, model mapping,
  routing ưu tiên, giới hạn upstream và cấu hình chi phí mua vào.
- Direct, proxy cố định, danh sách proxy và KiotProxy key-per-line.
- Giữ công cụ OAuth HTTP, batch, callback thủ công và export JSON hiện có.

### Ngoài phạm vi bản đầu

Thanh toán/nạp tiền tự động, hóa đơn thuế, marketplace, đăng ký tự phục vụ,
phân quyền nhiều cấp admin, embeddings, tạo ảnh/audio/video, realtime/WebSocket,
fine-tuning, Files/Assistants/Batch, thực thi tool/shell phía server, prompt
compression và tự thay model để tiết kiệm chi phí. Các phần này là đợt phát triển
riêng; không tạo endpoint giả trả success.

Ảnh **đầu vào** khác với tạo ảnh: cho phép trên tổ hợp adapter/model vision đã
kiểm thử; tổ hợp không hỗ trợ phải từ chối trước khi gọi upstream.

## 2. Kiến trúc được chọn

Chọn FastAPI chia module trong cùng repository, PostgreSQL cho trạng thái dịch vụ.
Gateway và quản trị có app factory/listener riêng, dùng chung các service/domain
module. Không yêu cầu Redis ở bản đầu; quota và lease phải đúng giữa nhiều process
nhờ transaction, row lock và fencing generation trong PostgreSQL.

Phương án tách gateway thành project/microservice riêng chưa chọn vì tăng công
triển khai và đồng bộ credential. Ghép hết vào app localhost cũ cũng không chọn:
middleware hiện tại yêu cầu Origin cho POST và chỉ cho phép localhost, không phù
hợp SDK khách hàng. Không nới middleware đó để vô tình công khai OAuth/export.

```text
Admin UI (private) ──> Admin API ──> cấu hình/version + secret vault
                                      │
Client OpenAI / Anthropic / Gemini / Ollama
    └─> Public gateway: xác thực key + chuẩn hóa giao thức
           └─> quyền model + quota reservation + concurrency lease
                  └─> router + credential + proxy lease
                         └─> upstream adapter ──> provider được cấu hình
                  <─ typed events/result + usage
           <─ định dạng đúng chuẩn client + quyết toán quota
```

Các ranh giới module dự kiến:

| Module | Trách nhiệm |
|---|---|
| `gateway/http` | App public, auth headers, kích thước body, request ID |
| `gateway/protocols` | Schema/decoder/encoder OpenAI, Anthropic, Gemini, Ollama |
| `gateway/providers` | Adapter outbound, capability, count/usage và lỗi upstream |
| `gateway/routing` | Resolve model, chọn route, affinity, cooldown |
| `gateway/accounts` | Credential upstream, OAuth refresh và generation |
| `gateway/proxy` | Profile proxy, pool, KiotProxy client và lease |
| `gateway/metering` | Usage normalization, reservation, settlement, audit |
| `gateway/storage` | PostgreSQL models, migrations, transaction/repository |
| `gateway/admin` | API quản trị, kiểm tra cấu hình và trạng thái |

Tên/thư mục cuối cùng được cụ thể hóa ở kế hoạch triển khai. Module inference
không gọi batch runner để đăng nhập khi nhận request của khách.

## 3. Khách hàng, API key và quản trị

Khách hàng có ID, tên hiển thị, ghi chú và trạng thái active/disabled. Tắt khách
chặn request mới của mọi key trực thuộc. Khách không có quyền xem khách khác.

Mỗi API key có ID/prefix công khai, tên, owner, trạng thái, thời hạn UTC, danh sách
model/protocol được phép, quota, RPM và concurrency. Secret ngẫu nhiên tối thiểu
256 bit, chỉ hiển thị đầy đủ khi tạo/xoay; lưu digest, không lưu plaintext có thể
khôi phục. Khách dùng key này ở gateway, không dùng token quản trị cũ.

- “Sửa key” sửa policy/metadata; đổi giá trị secret dùng thao tác rotate riêng.
- Rotate làm secret cũ mất hiệu lực và giữ nguyên quota/usage của key logic.
- Xóa là thu hồi và ẩn khỏi danh sách hoạt động, không xóa sổ usage/audit.
- Request mới kiểm tra policy từ nguồn dữ liệu có thẩm quyền; không dùng cache
  kéo dài hiệu lực key đã bị thu hồi. Request đã nhận tiếp tục tối đa deadline;
  UI ghi rõ revoke không thu hồi dữ liệu đã stream.
- Model allowlist mặc định rỗng là không được gọi model nào. Tùy chọn tất cả model
  phải là policy rõ ràng, không suy diễn từ trường bỏ trống.
- Chưa có ví quota dùng chung cho nhiều key; mỗi key có hạn mức riêng. Tổng hợp
  theo khách chỉ phục vụ báo cáo, tránh ngầm thay đổi công thức quota đã duyệt.

Admin tiếp tục xác thực bằng phiên quản trị riêng và CSRF cho mutation. UI/API
quản trị mặc định private/loopback; vận hành từ xa qua SSH/VPN. Gateway public
không mount route đăng nhập admin, OAuth, export hoặc file data. Public Internet
chỉ được bật khi cấu hình hostname/TLS/reverse proxy đã xác minh; không tự mở cổng.

## 4. Provider bên ngoài và cung cấp lại API

Đây là tính năng bản đầu, không chỉ là hướng mở rộng. Admin có thể tạo nhiều
provider dựa trên adapter có sẵn mà không chỉnh source:

| Adapter outbound | Cấu hình/giao thức |
|---|---|
| `openai_compatible` | Chat Completions và/hoặc Responses qua Base URL API |
| `anthropic` | Native Messages, count tokens khi upstream hỗ trợ |
| `gemini` | Native generateContent/stream/countTokens |
| `ollama` | Native chat/generate; endpoint private do admin cho phép |
| `codex_oauth` | Bộ token/account Codex đã nhập; contract riêng được kiểm thử |

Một provider gồm tên, adapter, API root, trạng thái, timeout, proxy profile,
credential pool, routing priority, model bindings, rate/concurrency limits và
thông tin giá mua vào. API root bao gồm prefix phiên bản nếu provider cần;
adapter ghép suffix cố định, không tự thêm trùng `/v1` hoặc nhận path từ client.
Không yêu cầu khách tự thêm provider ở bản đầu.

Credential là bản ghi riêng để đổi/thu hồi key mà không xóa model mapping. Key
upstream, custom secret header và OAuth token được mã hóa bằng AEAD, có key version;
master key nằm ngoài database. Chỉ truyền secret qua header/form đúng contract
adapter, không trả về UI/list API, không ghi log, không forward key khách upstream.
Auth scheme gồm Bearer, `x-api-key`, `x-goog-api-key` hoặc header được adapter cho
phép; no-auth chỉ được bật rõ ràng cho endpoint private được kiểm soát.

Thao tác admin: thêm/sửa/tắt/xóa provider, thêm/xoay credential, test kết nối,
lấy danh sách model, khai báo model thủ công và bật route. Xóa provider đang được
dùng là soft-delete; mapping bị vô hiệu, audit/usage vẫn giữ. Sửa root/auth/proxy
làm tăng config version; request đang chạy dùng snapshot cũ, request mới dùng bản
mới. Không chuyển secret sang host mới trong cùng request.

Model discovery chỉ tạo bản nháp để admin chọn, không tự public toàn bộ model
upstream. Probe mặc định không sinh nội dung; test inference tốn token chỉ chạy
khi admin bấm, được ghi là internal usage, không trừ quota khách.

Nhà cung cấp có giao thức khác năm adapter trên cần adapter mới và contract tests;
không hứa nhập bất kỳ URL nào cũng hoạt động. HTTP upstream thường yêu cầu HTTPS;
HTTP plaintext chỉ cho local/private origin được admin khai báo rõ, không chứa
credential truyền qua mạng công cộng.

### Model catalog và routing

- `public_model_id` là tên khách nhìn thấy. Một model public có một hoặc nhiều
  route tới model upstream; bindings ghi capability, context/output limit và
  tokenizer/usage policy. Có thể đặt alias, nhưng UI phải cho thấy model thật.
- Quyền được kiểm tra sau resolve alias; không tạo alias để lách allowlist.
- Chọn route enabled, đúng capability, credential khỏe và chưa vượt hạn mức; ưu
  tiên số nhỏ trước, round-robin giữa route ngang ưu tiên. Pin route theo phiên
  khi có continuation. Bản đầu không có định tuyến bằng AI hay trọng số tự học.
- Fallback mặc định chỉ giữa bindings của cùng model identity. Đổi sang model
  khác cần cấu hình alias/router model rõ ràng; không giả danh Claude/GPT chỉ vì
  request dùng chuẩn Anthropic/OpenAI. Khách phải biết alias là tên gateway.
- Tôn trọng 429/Retry-After, hard quota và trạng thái bị khóa của nhà cung cấp.
  Không xoay proxy/key để vượt hạn mức chung. Có cooldown theo credential hoặc
  provider tùy phạm vi lỗi; khi không xác định phạm vi, dùng provider cooldown.
- Tối đa hai attempt generation, chỉ khi upstream từ chối retryable rõ ràng hoặc
  chắc chắn chưa nhận body. Không retry khi không rõ request đã được thực thi,
  sau khi commit response client, hoặc với continuation bị ràng buộc route.
- Khi đổi route trước retry, kiểm tra lại capability, proxy và budget của route
  mới; tăng reservation atomically nếu upper bound lớn hơn. Không đủ hạn mức thì
  dừng, không dispatch bằng reservation của tuyến rẻ hơn. Hệ số khách vẫn giữ
  snapshot của public model tại admission, không thay theo chi phí nhà cung cấp.
- 401/403 và yêu cầu xác minh không chạy lại vô hạn. Token hết hạn chỉ được refresh
  theo quy tắc riêng ở mục 8; ban/quota không được coi là lỗi mạng.

“Cung cấp lại” là năng lực kỹ thuật, không phải quyền thương mại tự động. Admin
chịu trách nhiệm quyền dùng/phân phối của provider. Không dựa vào tên FreeLLMAPI
để khẳng định free tier được resale. Hệ thống không thực thi Codex CLI/shell thay
khách; OAuth adapter không đồng nghĩa subscription là OpenAI Platform API key.

## 5. Các chuẩn API vào/ra

Một key có thể được cấp nhiều chuẩn nhưng vẫn dùng **một** policy/quota ledger.
Inbound decoder và outbound adapter độc lập. Luồng Anthropic → Codex → Anthropic
là hợp lệ khi request nằm trong capability hỗ trợ, không biến model Codex thành
model Claude. Đích upstream do admin/router quyết định, không do URL trong body.

| Chuẩn | Endpoint bản đầu | Xác thực gateway | Output |
|---|---|---|---|
| OpenAI | `GET /v1/models`, `POST /v1/chat/completions`, `POST /v1/responses` | Bearer | JSON; SSE theo từng endpoint |
| Anthropic | `POST /v1/messages`, `POST /v1/messages/count_tokens`, `GET /v1/models` với version header | `x-api-key` hoặc Bearer, version được hỗ trợ | Messages JSON; SSE có event type |
| Gemini | `GET /v1beta/models`, `GET /v1beta/models/{model}`, POST `:generateContent`, `:streamGenerateContent`, `:countTokens` | `x-goog-api-key` hoặc Bearer | JSON; SSE khi `alt=sse` |
| Ollama | `POST /api/chat`, `POST /api/generate`, `GET /api/tags`, `POST /api/show`, `GET /api/version` | Bearer bắt buộc | JSON hoặc NDJSON |

Gateway dành `/api/*` ở bảng cho Ollama; admin ở listener khác nên không xung đột.
List/detail model cùng được lọc theo key và protocol; không trả model chưa được
phép, secret, đường dẫn credential hoặc endpoint upstream.

Không chấp nhận key trong query URL. Cho phép query không bí mật theo allowlist
endpoint, như `alt=sse` và phân trang model. Nếu nhiều header auth cùng xuất hiện
và không thống nhất, từ chối thay vì chọn một cách mơ hồ. Request không có Origin
của SDK được phép; CORS trình duyệt đóng mặc định và chỉ bật origin cấu hình.

### Contract chung và khác biệt

Canonical request/result giữ role, typed content blocks, system/developer
instructions, tool definitions, call IDs/arguments/results, generation controls,
finish reason và usage. Có typed streaming events thay vì chỉ gom text. Không
ép toàn bộ giao thức về chuỗi chat rồi bỏ mất tool IDs hay cấu trúc output.

- Core: text hội thoại nhiều lượt, system prompt, tool use/result round-trip,
  max output, stop controls hợp lệ và streaming/non-streaming.
- Adapter phải khai báo mapping cụ thể; không hỗ trợ ngữ nghĩa của tham số thì
  trả `unsupported_feature` trước dispatch. Không bỏ âm thầm ảnh, JSON schema,
  parallel tool calls, cache controls hoặc reasoning options.
- Tool chỉ là dữ liệu giao thức; gateway không chạy function/shell do model yêu cầu.
- Structured output, vision, reasoning summaries/opaque blocks chỉ bật khi toàn
  tuyến hỗ trợ và có kiểm thử. Không tạo thinking/signature giả; signed blocks
  của provider này không mang sang provider khác.
- OpenAI Chat dùng `choices/delta` và kết thúc đúng contract; Responses dùng các
  event `response.*`. Anthropic bảo toàn thứ tự `message_start`, content blocks,
  `message_delta`, `message_stop`; Ollama phát NDJSON hợp lệ và terminal record.
- Gemini core streaming chỉ quảng bá dạng SSE với `alt=sse`; biến thể streaming
  khác chưa hỗ trợ phải trả lỗi rõ. Các protocol version/beta không có trong
  compatibility matrix bị từ chối, không hứa hỗ trợ Claude Code/Codex mọi phiên bản.
- Count token dùng native counter hoặc tokenizer đã chứng minh phù hợp với model
  và serialization. Không có counter phù hợp thì trả unsupported, không tự đưa
  ước lượng ký tự thành số token chính xác. Endpoint count có RPM/concurrency và
  upstream budget riêng; không trừ quota generation của khách ở bản đầu.
- Response ID để continuation là handle gateway, scope theo customer + key +
  public model + route + credential. Chỉ hỗ trợ native continuation nếu upstream
  còn giữ state; lưu mapping mã hóa, không lưu prompt. Mapping hết hạn sau 24 giờ
  hoặc sớm hơn nếu upstream yêu cầu. Không share state giữa key; route mất thì
  báo lỗi, không fallback sang account khác. Tuyến không có continuation phải
  từ chối `previous_response_id` và yêu cầu client gửi đủ lịch sử.
- Responses chạy synchronous/SSE; không hỗ trợ background, retrieval/delete,
  compact, server-side stored conversation hoặc WebSocket ở bản đầu. Không bỏ
  qua `store:true` hay những tùy chọn ngoài contract; trả unsupported rõ ràng.

Compatibility matrix được xuất bản theo endpoint × capability × adapter × phiên
bản SDK đã test. “Có bốn chuẩn” không có nghĩa mọi tổ hợp tính năng đều hỗ trợ.

## 6. Quota, hệ số và chi phí

### Quy tắc được duyệt

```text
quota tiêu thụ = input_tokens × hệ số input
              + output_tokens × hệ số output
```

Ví dụ 1.000 input hệ số 1 và 500 output hệ số 3 tiêu thụ 2.500 token quy đổi.
Hệ số ưu tiên: ghi đè `(key, public model)` → mặc định model → 1/1.
Ghi đè thay thế hệ số, không nhân chồng. Hệ số hữu hạn, không âm, tối đa sáu chữ
số thập phân; giá trị 0 là miễn phí có chủ ý, UI cảnh báo nhưng vẫn có RPM/concurrency.

Sử dụng fixed-point micro-token (1 token quy đổi = 1.000.000 đơn vị), không dùng
float. Chuyển coefficient thành integer scale và nhân với token nguyên; database
dùng số nguyên độ chính xác đủ lớn với validation chống overflow.

- Lưu raw usage và normalized input/output riêng với weighted usage; không thay
  `usage` token thực của provider bằng token quy đổi trong response client.
- Cached input được tính một lần trong input, reasoning output một lần trong
  output. Adapter chuẩn hóa tổng inclusive/exclusive theo tài liệu từng provider;
  không cộng lại trường con đã nằm trong tổng. Cache read/write được giữ riêng
  để báo cáo chi phí nhưng không có hệ số quota riêng ở bản đầu.
- Hệ số, model binding và cost schedule được snapshot lúc admission; admin sửa
  giữa stream không thay giá request đang chạy hoặc viết lại lịch sử.
- Quota key gồm tổng tích lũy từ lúc tạo và cửa sổ ngày/tháng UTC. Null là không
  giới hạn, 0 là không còn quota. Không reset bằng cách xóa usage; admin điều chỉnh
  hạn mức bằng audit entry. Request tính vào cửa sổ lúc admission kể cả qua nửa đêm.
- Default key mới chưa có quota dương thì không gọi generation được. Admin phải
  cấp hạn mức hoặc chọn unlimited rõ ràng; RPM và concurrency vẫn bắt buộc hữu hạn.

### Reservation và settlement

Luồng accounting: `admitted → reserved → dispatched → completed/partial/failed`
hoặc `usage_pending`. Một transaction khóa row policy/buckets theo thứ tự cố định,
kiểm tra key/model, RPM, concurrency và quota rồi giữ hạn mức trước dispatch.
Không giữ transaction mở khi gọi HTTP. Tất cả gateway process dùng cùng ledger.
Model resolver chọn candidate binding/version trước khi tính reservation; router
chỉ dispatch candidate đó hoặc thực hiện bước điều chỉnh reservation ở trên.

Reservation cần input upper bound và output cap có thể thực thi của route. Ưu
tiên tokenizer/native counter đã chứng minh phù hợp; nếu không có upper bound
an toàn thì giữ theo context/input limit tối đa được chứng nhận của model, hoặc
từ chối route nếu model cũng không có giới hạn hữu hạn. Không gọi đoán rồi hy vọng
quota đủ. UI phải giải thích trường hợp reservation bảo thủ khiến request bị từ chối.

Client bỏ max output thì dùng cap được admin cấu hình nếu upstream thực thi được.
Mọi cap khách yêu cầu phải được adapter bảo toàn; upstream không hỗ trợ thì trả
unsupported, không lặng lẽ bỏ tham số. Tuyến như một số Codex OAuth contract không
có cap tùy chỉnh chỉ dùng được khi có output bound hữu hạn được kiểm chứng của
model: giữ trước theo bound đó và ghi rõ giới hạn tương thích. Cắt stream local
không được coi là bằng chứng upstream đã ngừng tính token, nhất là reasoning.
Không có bound hợp lệ thì route không đủ điều kiện admission. Thiếu quota cho
reservation thì reject trước dispatch, không âm thầm cắt output cap của khách.

- Settlement duy nhất theo request ID/ledger constraint: trừ actual weighted
  usage rồi hoàn lại reservation thừa trong transaction. Mỗi attempt upstream có
  usage/cost riêng nhưng request khách không bị charge hai lần vì retry nội bộ.
- Lỗi trước dispatch hoặc upstream từ chối chắc chắn không phát sinh generation:
  release hold, quota khách bằng 0. Chi phí probe/retry lỗi nội bộ do dịch vụ chịu.
- Partial output đã trả khách: nếu có authoritative usage, quyết toán phần attempt
  đã phục vụ; nếu không, chuyển `usage_pending`. Usage từ attempt trước chưa commit
  response được ghi chi phí upstream, không cộng vào quota khách thành công sau đó.
- Timeout không rõ upstream đã xử lý, stream đứt thiếu usage, crash hoặc commit
  settlement thất bại: giữ reservation ở `usage_pending`, không giả usage=0 và
  không coi toàn bộ hold là phí chính xác. Background reconciliation chỉ finalize
  tự động khi có bằng chứng usage; quá 24 giờ cảnh báo admin, không tự xóa hold.
- Admin có thể quyết toán/hoàn hold bằng adjustment có lý do, actor và nguồn
  `admin_adjusted`; không sửa raw usage hoặc gắn nhãn là provider-reported.
- Gateway không nhận request mới khi PostgreSQL không ghi reservation được.
  Nếu database lỗi giữa stream, dừng/cancel upstream, ghi trạng thái pending khi
  phục hồi; không phát completed giả. Lease concurrency có thể giải phóng sau
  deadline, nhưng hold quota không được giải phóng chỉ vì lease hết hạn.
- Nếu provider trả usage vượt upper bound đã cam kết, lưu raw usage và sự cố;
  charge khách tối đa reservation, phần chênh dịch vụ chịu, cô lập route để kiểm
  tra. Không làm quota khách âm hoặc im lặng cắt số usage thực.

Idempotency-Key tùy chọn được scope theo key, giữ 24 giờ; hash payload có HMAC,
không lưu prompt. Key trùng trả conflict (`in_progress`/`already_processed`/
`payload_mismatch`), không gọi lại provider hoặc charge thêm. Bản đầu không replay
body/stream đã hoàn tất. Client không gửi idempotency key tạo request mới bình thường.

### Chi phí mua vào khác quota khách

Admin nhập đơn giá upstream trên một triệu input/output và tùy chọn cache
read/write, currency, thời điểm hiệu lực. Normalizer tách đúng bucket tránh tính
cached input hai lần. Nếu không có giá/usage thì cost là unknown, không mặc định 0.
Ghi `estimated` cho cost suy từ bảng giá; chỉ gọi là actual khi có chứng từ/provider
report phù hợp. Bản đầu không quy đổi ngoại tệ hoặc gọi chênh lệch là lợi nhuận.

Provider/credential có budget đầu vào độc lập với quota khách; reservation budget
cũng transactional nếu được bật. Route có hard money budget chỉ admission khi có
upper bound chi phí hợp lệ. Khi không còn route hợp lệ trả unavailable, không tự
vượt budget hoặc chuyển sang provider ngoài danh sách.

## 7. Proxy và KiotProxy

### Cấu hình

| Mode | Đầu vào và lựa chọn |
|---|---|
| `direct` | Không qua proxy; bỏ ảnh hưởng proxy environment |
| `fixed` | Một HTTP/HTTPS/SOCKS5/SOCKS5H proxy có auth tùy chọn |
| `pool` | Một proxy mỗi dòng; validate/dedup; round-robin slot rảnh |
| `kiotproxy` | Một API key mỗi dòng; dedup; region `random/bac/trung/nam`, protocol HTTP hoặc SOCKS5 |

“Proxy cố định” và “direct không proxy” là hai mode riêng, tránh nhập nhằng cụm
direct proxy. Giữ parser định dạng proxy cũ; không tự fallback direct khi nhập sai.

Admin có profile mặc định và override theo provider/credential upstream. OAuth
batch có thể chọn profile trước khi chạy. OAuth và inference có lựa chọn riêng,
nhưng credential mới nhập kế thừa profile OAuth trừ khi admin chủ động đổi; refresh
dùng profile được gắn với credential. Client không được truyền proxy/host tùy ý.

Proxy lỗi trả lỗi theo giai đoạn: DNS/connect/TLS/proxy auth/timeout; không đánh
dấu account sai password chỉ vì proxy chết. Credential proxy/Kiot được mã hóa;
UI chỉ hiện nhãn và trạng thái an toàn, secret là write-only.

### Lease và IP ổn định

Shared lease manager điều phối cả OAuth, refresh và inference; không dùng một lock
chỉ có hiệu lực trong một thread/process khi dịch vụ được bật. OAuth standalone
không có gateway dùng lease manager in-memory trong coordinator duy nhất; Kiot
keys nhập ở chế độ này chỉ giữ RAM. Khi bật dịch vụ, cả OAuth và gateway bắt buộc
dùng PostgreSQL lease chung, không được dùng đồng thời hai backend lease trên
cùng pool. Fixed/pool mặc định một slot/request mỗi
proxy; admin có thể tăng concurrency cho proxy cố định không tự đổi IP. KiotProxy
bản đầu giữ **một hoạt động tại một thời điểm trên mỗi key**, nên nhiều key mới tăng
được mức song song mà không đổi IP của request khác.

Giữ nguyên proxy qua attempt OAuth, refresh hoặc response stream. Pool chỉ chọn
lại ở request/attempt mới hợp lệ; không đổi IP giữa chừng để cứu stream. Affinity
account/conversation cố gắng giữ cùng profile/member giữa request, nhưng không
cam kết một địa chỉ IP tồn tại vĩnh viễn. Nếu upstream yêu cầu IP bền vững, admin
phải chọn fixed proxy; Kiot rotation có thể không phù hợp.

Mỗi lease có owner, generation/fencing và hard deadline. Worker không gia hạn
được phải ngừng gọi upstream. Sau mất heartbeat, allocator không đổi IP ngay:
chờ hard deadline của operation + 15 giây grace. Deadline được thực thi cả ở HTTP
client; process cũ không được ghi đè credential/lease mới sau khi mất quyền sở hữu.

### Vòng đời KiotProxy

Control client riêng chỉ gọi `https://api.kiotproxy.com/api/v1/proxies`, TLS bật,
không redirect, không qua chính proxy đang cấp; không nới allowlist OAuth cũ.
Kiot buộc key ở query: client phải tắt/redact URL trong mọi log, exception và trace.
Không trả raw message provider vì message lỗi có thể chứa key đầy đủ.

1. Giữ lease theo fingerprint key toàn hệ thống, kiểm tra cache và `/current`.
2. Có current proxy còn đủ thời gian sống cho request deadline + 15 giây thì tái
   sử dụng; mặc định không gọi `/new` cho mỗi request.
3. `PROXY_NOT_FOUND_BY_KEY` cho phép xin `/new`. Key sai/hết hiệu lực được vô hiệu
   trong scheduler và báo lỗi rõ, không retry cho từng account liên tiếp.
4. Chỉ `/new` khi không còn hoạt động dùng key và cooldown cho phép. Admin có tùy
   chọn rotate-between-operations, mặc định off; auth rejection không kích hoạt rotate.
5. Tính thời điểm an toàn từ `ttc`, `nextRequestAt`, `expirationAt`, `ttl`, timestamp
   response; lấy ràng buộc bảo thủ khi khác nhau, dùng monotonic deadline nội bộ.
   Schema sai, thời gian thiếu/mâu thuẫn không xác định được lease thì từ chối.
6. Cooldown chưa hết: dùng current đủ TTL; nếu không, chờ có thể hủy trong acquisition
   timeout tối đa 10 giây. Quá timeout trả `proxy_not_ready` và Retry-After khi biết.
   Thời gian chờ không trừ deadline OAuth sau khi được cấp slot; tổng request gateway
   vẫn bị giới hạn wall-clock. Không sleep loop gọi API dồn dập.
7. `/new` timeout không rõ kết quả: đọc `/current` để đối chiếu trước khi cân nhắc
   cấp tiếp. Provider 429 được tôn trọng; không giải phóng/đổi key để lách cooldown.
8. `/out` chỉ khi admin yêu cầu rõ và key đang idle. Dừng job/tool không tự `/out`.
9. Kiểm tra endpoint proxy trả về; không đưa các host private/metadata bất ngờ từ
   provider vào luồng egress. Trùng endpoint giữa các key vẫn chịu giới hạn slot
   endpoint đã cấu hình. Key chia sẻ ngoài hệ thống có thể bị đổi IP ngoài tầm
   kiểm soát; UI cảnh báo nên dành riêng key cho dịch vụ.

HTTP client không thể bảo đảm proxy bên thứ ba không tự đổi IP hoặc chết. Khi đó
abort request, báo lỗi/pending usage đúng trạng thái; không giả thành công.

## 8. Codex OAuth và dữ liệu cũ

Giữ nguyên format `success.json` hiện tại. Admin chọn import từ batch thành công
hoặc JSON; không tự quét toàn bộ data/account.txt hoặc đưa tất cả account vào pool
public. Import validate schema/identity, không coi decode JWT không kiểm tra là
bằng chứng quyền sở hữu. Credential nhập có thể ở trạng thái cần kiểm chứng trước
khi enabled; dedup theo issuer/account/workspace, không chỉ email.

Gateway dùng credential store mã hóa, không đọc trực tiếp `run.json` cho mỗi request.
Các export/run cũ vẫn chứa token plaintext được bảo vệ bằng quyền file; không
tuyên bố chúng tự được mã hóa sau nâng cấp, không xóa/sửa file gốc. UI cảnh báo
bản export là bí mật, admin chỉ tải qua private interface.

Refresh là single-flight theo credential bằng lease + generation trong database.
Lưu toàn bộ token chain mới atomically; không cho response refresh muộn ghi đè
generation mới. `invalid_grant` chuyển `reauth_required`; timeout không rõ token
đã xoay thì `refresh_uncertain`, không lặp grant cũ liên tục. Refresh không tự
đăng nhập bằng password/TOTP hoặc chạy hàng loạt account.

OAuth adapter là contract riêng với upstream, không dùng token subscription để
giả API key Platform. Phải có live contract test được cho phép trước khi coi route
là production-ready; chưa có credential thì trạng thái là unverified, không bịa
kết quả. Không vượt email/phone/CAPTCHA verification hoặc hard restriction.

CheckLive hiện có là dependency local chưa xác định quyền phân phối (xem
`THIRD_PARTY.md`). Gateway mới không import hay đóng gói dependency đó. Local
batch cũ giữ nguyên; deployment dịch vụ có thể dùng OAuth thủ công/import mà không
cài CheckLive. Không tự gán license mới cho source của người khác.

## 9. Lưu trữ và bảo mật

Các nhóm bảng chính:

| Nhóm | Dữ liệu |
|---|---|
| Identity/policy | customers, api_keys, key_model_policies, config_versions |
| Routing | providers, credentials, public_models, model_bindings, model_aliases |
| Proxy | proxy_profiles, proxy_members, proxy_leases, proxy_health |
| Metering | requests, attempts, quota_buckets, reservations, usage_ledger, adjustments |
| Operation | concurrency_leases, cooldowns, continuation_handles, audit_events |

Secret cipher có nonce, key ID/version và authenticated associated data gồm loại
bản ghi + ID; không tái dùng nonce, không dùng encrypted value làm lookup key.
Fingerprint/digest secret dùng domain separation; Kiot dedup toàn deployment.
Audit không chứa secret trước/sau. Master key rotation và backup/restore phải giữ
khả năng giải mã bản cũ; mất master key được báo rõ, không tự thay key để chạy tiếp.

Host/root/proxy chỉ admin cấu hình. SSRF guard chặn loopback, link-local, metadata,
private IP mặc định cả IPv4/IPv6; internal Ollama/proxy chỉ qua allowlist rõ từng
origin/CIDR do operator cấu hình. Kiểm tra DNS ở connection, redirect và model
discovery; không forward auth sang host khác. Với proxy resolve DNS từ xa, không
thể chứng minh đích bằng DNS local: cần egress policy ở proxy/firewall, allowlist
origin và proxy tin cậy. Tài liệu không được tuyên bố đã chặn DNS rebinding chỉ nhờ
validate hostname trước request.

Client remote image/document URL không được gateway tùy ý fetch. Bản đầu dùng
inline content hoặc native URL forwarding trên route được cấp capability; không
cho URL tham chiếu nội bộ/metadata vượt qua policy. Không có endpoint generic HTTP
proxy hay arbitrary headers/body relay.

Không ghi prompt/completion/tool arguments vào log mặc định. Persist request ID,
key ID, customer ID, model alias, provider/credential ID, latency/TTFT, stage,
status, usage/cost và mã lỗi an toàn. Không trả account email, proxy host hay raw
provider body cho khách. Retention metadata mặc định 90 ngày; aggregate/ledger và
audit giữ đến khi operator có policy lưu trữ riêng, không xóa cùng key.

Body gateway tối đa 16 MiB trước parsing; SSE frame 1 MiB; tổng tool arguments
1 MiB/request; số tool calls tối đa 128. Request wall-clock mặc định 180 giây, admin
được cấu hình tối đa 600 giây; connect timeout 10 giây, upstream stream idle timeout
60 giây. Backpressure bounded, không buffer cả stream vô hạn. Không log query raw.

## 10. Streaming, lỗi và phục hồi

Không commit HTTP 200/SSE trước khi upstream mở response hợp lệ. Trước commit trả
HTTP status và error envelope theo chuẩn client; sau commit chỉ phát error event
đúng chuẩn và đóng stream, không giả completed và không chuyển model giữa stream.

Client disconnect thì cancel upstream, ngừng nhận nội dung ngoài phần cần đóng
kết nối; không tiếp tục generation không giới hạn chỉ để lấy usage. Cleanup luôn
giải phóng slot hoạt động; accounting vẫn pending nếu usage thiếu.

| Nhóm lỗi | HTTP trước commit / hành vi |
|---|---|
| Key không hợp lệ/hết hạn/thu hồi | 401 theo envelope protocol |
| Key không có quyền model/protocol | 403; model discovery không tiết lộ mục ẩn |
| Input/schema/tính năng không hỗ trợ | 400; field/code an toàn |
| Quota/RPM/concurrency của khách | 429; Retry-After nếu có thời điểm xác định |
| Hết route do cooldown/budget/proxy chưa sẵn sàng | 503; mã lỗi phân biệt |
| Provider/proxy auth/TLS/protocol lỗi | 502; chi tiết nhạy cảm chỉ bản admin đã lọc |
| Deadline upstream | 504 trước commit; stream error sau commit |
| PostgreSQL/secret store không sẵn sàng | 503, fail closed admission |

Error object và request ID theo contract từng protocol; HTTP status có thể map
riêng khi chuẩn yêu cầu, nhưng mã domain trong log giữ nhất quán. Hạn mức tổng
không reset thì không bịa Retry-After. Upstream 401 không bị trả như key khách sai.

Health public chỉ liveness/readiness tối thiểu, không hiện provider/account/model.
Worker khởi động lại phục hồi request/hold chưa settlement từ database; không tự
replay generation. Không có thành công giả khi lưu ledger thất bại.

## 11. Giao diện và API quản trị

Giữ hướng giao diện tiếng Việt hiện tại; phần này là yêu cầu chức năng, không phải
bản thiết kế pixel/canvas. Thiết kế UI chi tiết được duyệt riêng trước khi sửa UI.

- Tổng quan: request/usage/quota theo thời gian, lỗi, pending holds, health.
- Khách hàng/API key: tạo/xoay/khóa/thu hồi, expiry, model/protocol policy, quota,
  RPM/concurrency; copy key chỉ tại lần tạo, xác nhận thao tác nguy hiểm.
- Model: public ID, aliases, capabilities, route bindings, coefficient defaults
  và overrides; preview model mà một key thực sự nhìn thấy.
- Provider: adapter/root/credential pool, model discovery, test kết nối, bảng giá,
  budget mua vào, proxy, ưu tiên và trạng thái verified/unverified.
- OAuth/account: import có chọn lọc, profile proxy, refresh/reauth state; không
  hiển thị raw tokens trong bảng. Batch/export cũ vẫn private.
- Proxy: bốn mode, Kiot keys mỗi dòng, region/protocol, cooldown/expiry, release
  có xác nhận khi idle. Test proxy không đăng nhập account.
- Usage/audit: lọc theo key/customer/model/provider; actual/estimated/unknown
  rõ ràng; raw token và weighted quota riêng; pending hold có hành động đối soát.
- Playground: chọn protocol/model/key thử nghiệm; ghi usage đúng key được chọn.
  Không nhúng master/admin/upstream key vào snippet của khách.

Admin mutations dùng version để phát hiện cập nhật đè nhau và transaction để ghi
audit cùng thay đổi. Mọi endpoint CRUD có tests truy cập trái quyền và redaction.
Nếu tên giá trị do người dùng nhập xuất hiện trong log/UI thì escape và giới hạn
độ dài; tránh nội dung HTML hay control character trở thành nguồn rò rỉ/injection.

## 12. Triển khai và tương thích ngược

Lệnh chạy local hiện tại giữ hành vi mặc định. Gateway là chế độ opt-in, port riêng
(mặc định 8788 loopback), không khởi động theo ngầm định khi chỉ chạy OAuth tool.
PostgreSQL chỉ bắt buộc khi bật dịch vụ; chế độ OAuth cũ vẫn đọc/ghi data như trước.
OAuth coordinator hiện tại giữ một process sở hữu RunStore; tăng số worker gateway
không tạo nhiều coordinator cùng đọc data cũ.

Triển khai dịch vụ gồm gateway, private admin/maintenance worker, PostgreSQL và
TLS reverse proxy. Một leader lease chạy cleanup/reconciliation; nhiều gateway
worker dùng chung DB constraints, không lấy in-memory lock làm bảo đảm tài chính.
Không tự đổi DNS, public bind, firewall hay cấu hình client Codex trên máy người dùng.

Migrations có version, có backup/restore drill bằng dữ liệu giả. Không destructive
migration hoặc tự import credential thật. Rollback ứng dụng không xóa ledger;
migration theo hướng additive cho bản đầu. Chi tiết cấu hình/migrations thuộc
kế hoạch triển khai sau khi đặc tả được duyệt.

## 13. Kiểm thử và điều kiện nghiệm thu

1. Contract fixtures độc lập cho bốn chuẩn inbound và năm adapter outbound;
   tổ hợp core khả thi được test cả JSON và stream, không cần credential thật.
   Mỗi fixture có nguồn/tình huống, không copy nguyên test source của reference.
2. SDK integration: OpenAI (Chat/Responses), Anthropic, Google GenAI, Ollama với
   version pin trong test environment; version được ghi vào compatibility matrix.
   Kiểm thử tool-call round-trip, Unicode/frame split, empty output, finish reason,
   usage missing, unsupported options và lỗi sau khi stream đã commit.
3. Auth/tenant isolation: thiếu/sai/thu hồi key, conflicting headers, model alias
   bypass, count/model discovery, continuation cross-key và admin access từ gateway.
4. PostgreSQL integration với nhiều worker thực: quota race, reservation rollback,
   midnight UTC, coefficient change, duplicate settlement/idempotency, DB outage,
   crash/restart, lease fencing và pending reconciliation; không thay bằng SQLite
   rồi khẳng định đã chứng minh transaction PostgreSQL.
5. Provider CRUD/discovery/probe/mapping, root đổi khi request chạy, auth secret
   không rò rỉ, route unhealthy, same-model fallback, hard quota và uncertain retry.
6. Proxy HTTP/HTTPS/SOCKS auth, pool dedup, env isolation; Kiot current/new/out,
   bad key, cooldown/TTL, concurrent OAuth/inference, cancellation, timeout cấp mới
   không rõ kết quả, duplicate endpoint và lỗi message chứa secret.
7. Security: host/origin/TLS boundary, body limits, SSRF/DNS/redirect, secret at rest,
   encrypted backup, log/UI/export redaction và không forward key khách upstream.
8. UI Node/browser regression; test local OAuth hiện tại, JSON export schema,
   request bootstrap và kết quả cũ không bị thay đổi.
9. Live smoke chỉ dùng credential được operator cho phép, giới hạn request/token;
   không tự chạy tất cả account hoặc gọi Kiot bằng key ví dụ. Nếu chưa có credential,
   báo rõ live-unverified; không suy success từ tests giả lập.

Một route chỉ hiện “verified” sau kết quả test thực phù hợp; bật thương mại là
quyết định vận hành riêng, không được suy từ OAuth thành công. Không phát hành
gateway như đã hỗ trợ toàn bộ FreeLLMAPI/Cockpit/9router khi matrix chưa chứng minh.

## 14. Thứ tự phát triển và cửa duyệt

Đây là một phân hệ gateway có các phần phụ thuộc, không phải ba sản phẩm ghép
source. Đợt bản đầu đi theo lát dọc: persistence/key/quota → OpenAI route hoạt động
end-to-end → provider/proxy/OAuth → các adapter còn lại → admin UI/hardening và
compatibility tests. Các chuẩn đã duyệt đều thuộc bản đầu, không bị âm thầm bỏ.
Đây chỉ là thứ tự kiến trúc; task/file/test cụ thể sẽ nằm ở implementation plan.

Sau duyệt tài liệu này mới viết kế hoạch triển khai. Người dùng duyệt kế hoạch và
chọn cách thực hiện trước khi thay đổi product code/dependencies. Không coi phản
hồi “được” trước khi tài liệu tồn tại là đã duyệt cả spec và plan này.

## 15. Nguồn tham khảo và mức chắc chắn

- Codebase hiện tại tại commit `337a72e`: `api.py`, `security.py`, `runner.py`,
  `proxies.py`, `transport.py`, `tokens.py`, `models.py`, `THIRD_PARTY.md`.
- [FreeLLMAPI API reference](https://github.com/tashfeenahmed/freellmapi/blob/1346b7d39ecbc71ac4c087fcdd36248ebdbde79b/docs/en/api/01-rest-api.md),
  commit `1346b7d39ecbc71ac4c087fcdd36248ebdbde79b`: nguồn tham khảo giao thức,
  không phải chứng nhận chất lượng/thương mại cho sản phẩm này.
- [Cockpit Codex API Service](https://github.com/jlcodes99/cockpit-tools/blob/79870647b7d3403da037cd0064bccb5092822743/docs/CODEX_API_SERVICE_HANDOFF.md),
  commit `79870647b7d3403da037cd0064bccb5092822743`. README revision này công bố
  CC BY-NC-SA 4.0; không tái sử dụng source/UI/sidecar của Cockpit.
- [9router](https://github.com/decolua/9router/tree/39e36d3d0c849e0e01dfeacddf111edf892448fc),
  commit `39e36d3d0c849e0e01dfeacddf111edf892448fc`: tham khảo README về model routing
  và chuyển định dạng. Chưa audit toàn bộ source; không lặp lại lời hứa unlimited.
- `../Auto-Oauth-Codex-9Router/README.md`: workflow tham khảo local; không sửa project.
- [OpenAI streaming](https://developers.openai.com/api/docs/guides/streaming-responses):
  phân biệt Chat chunks và Responses semantic events.
- [OpenAI authentication](https://developers.openai.com/codex/auth/): phân biệt
  ChatGPT/Codex subscription và Platform API key; tài liệu không xác lập quyền
  resale subscription dưới một dịch vụ API chung.
- KiotProxy: API `/new`, `/current`, `/out` và field schema do người dùng cung cấp
  trong hội thoại. Chưa có API key thật để kiểm thử, không có live call trong bước
  thiết kế. Adapter phải xác minh schema/cooldown bằng fixtures và smoke được phép.

Các chi tiết xác thực/version/capability của Anthropic, Gemini, Ollama và Codex
outbound phải đối chiếu tài liệu chính thức/contract hiện hành lúc triển khai và
pin trong compatibility tests; không suy đầy đủ semantics chỉ từ FreeLLMAPI.
