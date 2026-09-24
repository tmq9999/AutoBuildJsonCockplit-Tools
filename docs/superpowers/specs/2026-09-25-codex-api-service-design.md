# Codex API Service — Thiết kế triển khai riêng

## Trạng thái

Thiết kế này được xây dựng sau khi đối chiếu tài liệu handoff và implementation
công khai của Cockpit Tools tại commit `79870647b7d3403da037cd0064bccb5092822743`.
Chỉ hành vi và giao thức tương thích được tham khảo; không sao chép source, UI,
sidecar hay cấu trúc dữ liệu của Cockpit.

## Mục tiêu

Xây dựng một dịch vụ API Codex riêng trong AutoBuildJsonCockplit-Tools. Client
OpenAI/Codex có thể dùng một API key gateway và gọi endpoint tương thích OpenAI,
trong khi admin quản lý tài khoản OAuth, account pool, routing, proxy, quota,
usage và reset-credit của OpenAI.

Dịch vụ phải dùng PostgreSQL làm nguồn sự thật cho cấu hình và kế toán. Secret
OAuth, API key upstream, KiotProxy key và client key không được trả lại trong
API list, log hay UI; chỉ được hiển thị một lần khi tạo/rotate client key.

## Phạm vi tương thích Cockpit

Cockpit Codex API Service cung cấp một local authenticated gateway; nó không phải
một hosted API của Cockpit. Bản triển khai này giữ cùng ý nghĩa ở mức public API,
nhưng chạy FastAPI và adapter riêng:

| Capability | Bản đầu | Quy tắc |
|---|---:|---|
| `GET /v1/models` | Bắt buộc | Chỉ trả model enabled và policy cho key |
| `POST /v1/chat/completions` | Bắt buộc | JSON/SSE, idempotency, usage normalized |
| `POST /v1/responses` | Bắt buộc | JSON/SSE, continuation nếu route hỗ trợ |
| `POST /v1/responses/compact` | Có điều kiện | Chỉ bật khi adapter có contract test; nếu không trả `unsupported_feature` |
| `GET/POST /backend-api/codex/*` | Có điều kiện | Không proxy tùy ý; chỉ route allowlist đã khai báo |
| Responses WebSocket | Giai đoạn sau | Không mở cổng nếu chưa có auth, quota và close/error tests |
| Image generation/edit | Giai đoạn sau | Pool slot riêng theo account, không dùng session affinity của text |
| Anthropic/Gemini/Ollama | Giữ nguyên | Dùng gateway protocol hiện có, không làm thay đổi public contract |

`/backend-api/codex/*` không phải API công khai ổn định. Adapter phải có
capability/version flag, timeout, allowlist path và lỗi `unsupported_feature`
khi upstream thay đổi. Không giả vờ hỗ trợ mọi endpoint của ChatGPT.

## Kiến trúc

```text
Client OpenAI/Codex
    -> FastAPI gateway (/v1/*)
    -> client-key auth + model policy + quota reservation
    -> account-pool router + session affinity + cooldown
    -> proxy lease (direct/fixed/pool/KiotProxy)
    -> Codex OAuth adapter
    -> chatgpt.com/backend-api/codex (allowlist)
    -> response/usage normalizer + quota settlement + audit
```

Các ranh giới code:

- `http/app.py`: public endpoint và protocol error mapping.
- `providers/codex.py`: Codex upstream request, header allowlist, response stream.
- `accounts/imports.py`, `accounts/refresh.py`: credential/token lifecycle.
- `routing/`: binding, account selection, session affinity và retry budget.
- `metering/`: raw usage, normalized usage, weighted customer quota và upstream cost.
- `proxy/`: direct/fixed/pool/KiotProxy lease.
- `admin/`: CRUD cấu hình, tài khoản, pool, usage, reset-credit và audit.
- PostgreSQL migrations: account metadata, quota snapshot/reset records, pool policy,
  session binding và usage indexes.

## Account model và OAuth lifecycle

Mỗi tài khoản OAuth là một credential đã import từ record hợp lệ. Tối thiểu lưu:

- `credential_id`, `account_id`, email đã chuẩn hóa, provider/profile;
- health: `unverified`, `active`, `refreshing`, `reauth_required`,
  `refresh_uncertain`, `disabled`;
- token generation, expiry và encrypted token payload;
- plan type, quota snapshot, quota error và `quota_updated_at`;
- proxy profile và routing eligibility;
- cooldown/error counters, last success và last failure reason.

Refresh dùng lease PostgreSQL, chỉ một worker được refresh một credential. Token
expired/401 được refresh tối đa theo một retry policy; refresh không chắc chắn
chuyển tài khoản sang `refresh_uncertain` và loại khỏi pool cho đến khi admin
refresh/verify lại.

## Account pool và routing

Một public model có thể có nhiều binding tới credential Codex. Router hỗ trợ:

1. `auto`: chọn account khỏe, quota còn, không cooldown; ưu tiên quota/plan/expiry
   theo policy đã lưu.
2. `random`: shuffle các account đủ điều kiện cho request mới.
3. `single`: khóa vào một credential đã chọn.
4. `priority`/`weight`: chọn theo thứ tự hoặc trọng số, fallback bounded.

Session affinity bật mặc định cho conversation/continuation đã có. Binding session
được khóa theo customer key + model + continuation scope; request mới không được
đổi account giữa stream. Request image, khi được bật, bỏ qua text session affinity
và dùng slot riêng theo account.

Retry tối đa một lần sang credential khác khi upstream từ chối trước khi sinh
output và lỗi đó an toàn để retry (ví dụ 429 có Retry-After). Không retry timeout,
partial stream hoặc request đã có generation. Mọi retry dùng cùng request ledger
và chỉ một lần charge.

## Quota OpenAI và nút Reset quota

Quota OpenAI của từng account là dữ liệu upstream, không phải quota khách hàng.
Adapter gọi bằng access token mới nhất và proxy profile của credential:

- `GET /backend-api/wham/usage`: lấy plan, primary window, secondary window,
  `used_percent`, `reset_after_seconds`/`reset_at`.
- `GET /backend-api/wham/rate-limit-reset-credits`: lấy các lượt reset còn có thể
  dùng, trạng thái và thời điểm hết hạn.
- `POST /backend-api/wham/rate-limit-reset-credits/consume`: dùng một lượt reset,
  body `{ "redeem_request_id": "<uuid>" }`; sau đó bắt buộc refresh lại usage.

Admin UI phải hiển thị:

- phần trăm còn lại của primary/secondary window;
- reset time hoặc `Không rõ` nếu upstream không cung cấp;
- số reset-credit còn lại và expiry gần nhất;
- thời điểm snapshot và lỗi gần nhất.

Nút `Đặt lại quota OpenAI` phải:

1. fetch reset-credit snapshot;
2. yêu cầu xác nhận nếu available count bằng 0 thì disable;
3. tạo UUID request mới, không retry POST với UUID khác;
4. gửi consume qua proxy của account;
5. refresh usage và reset-credit snapshot;
6. ghi audit `oauth.quota_reset_requested` với credential id, upstream status,
   request id và kết quả; không ghi access token hoặc raw response body.

Nếu consume thành công nhưng refresh thất bại, UI phải báo “reset đã gửi, chưa
xác nhận được snapshot mới” và khóa nút cho đến khi fetch lại thành công. Không có
đường tắt reset vô hạn, không tự sửa `used_percent` trong database.

## Customer API key và quota 100 triệu token

Customer quota vẫn do gateway quản lý riêng với quota OpenAI. `KeyPolicy` giữ
total/day/month, RPM, concurrency, model/protocol allowlist và model override.

Usage chuẩn hóa thành các bucket không chồng lấn:

```text
uncached_input = inclusive_input - cached_read - cached_write
weighted_quota = uncached_input × input_rate
               + cached_read × cache_read_rate
               + cached_write × cache_write_rate
               + output × output_rate
```

Quy tắc chuẩn hóa:

- Nếu provider báo `input_tokens` đã bao gồm cached tokens, trừ cached trước khi
  tính `uncached_input`.
- Nếu provider báo input không bao gồm cache, cộng cache vào inclusive input rồi
  phân bucket như trên.
- Không cộng lại reasoning nếu reasoning đã nằm trong output.
- Default `cache_read_rate` và `cache_write_rate` bằng `input_rate`; admin có thể
  đặt giá riêng ở cost schedule, nhưng policy cũ vẫn tương thích.
- Response client giữ raw provider usage; weighted quota chỉ nằm trong ledger.
- Với hạn mức 100.000.000 token, reservation và settlement dùng fixed-point
  integer micro-token, không dùng float. Cache không bị tính hai lần.

Upstream cost có thể dùng rate riêng cho input/cache-read/cache-write/output.
Customer quota và upstream cost không trộn chung; lỗi probe/retry nội bộ không
trừ quota khách.

## Proxy

Mỗi provider/credential/profile có thể chọn:

- `direct`: không proxy;
- `fixed`: một HTTP/SOCKS5 endpoint;
- `pool`: pool endpoint tuần tự/round-robin;
- `kiotproxy`: mỗi dòng một KiotProxy key, gọi `/proxies/current` hoặc `/proxies/new`
  theo region, giữ lease và `/proxies/out` khi không còn sử dụng.

Proxy secret chỉ fingerprint trong DB. Mọi upstream request và quota refresh/reset
đều phải đi qua lease được cấp cho credential. Kiot key không xuất hiện trong
response admin, error, audit detail hoặc log.

## Public protocol và lỗi

OpenAI client gửi `Authorization: Bearer <gateway-key>`. Gateway không chấp nhận
client gửi trực tiếp access token OpenAI. Error mapping giữ `error.code`, HTTP
status, `Retry-After` khi biết được; không biến quota, reauth, proxy và network
failure thành cùng một lỗi.

SSE phải giữ response id, event ordering và usage cuối stream. Nếu stream đã
commit header mà upstream fail, gửi SSE error event và kết thúc stream; không đổi
HTTP status sau khi commit.

## Admin API/UI

Bổ sung các nhóm endpoint private dưới `/api/service`:

- `GET /oauth-accounts`, `GET /oauth-accounts/{id}/quota`;
- `POST /oauth-accounts/{id}/quota/refresh`;
- `GET /oauth-accounts/{id}/reset-credits`;
- `POST /oauth-accounts/{id}/reset-credits/consume`;
- `GET/PUT /account-pools/{model_id}`;
- `GET /usage/accounts`, `GET /usage/requests`;
- `POST /keys/{id}/quota-adjust` cho điều chỉnh gateway quota có audit, không xóa
  ledger;
- service state/health và capability matrix.

UI giữ matte dark-bronze admin hiện tại, thêm tab “Codex accounts” với bảng email
đã mask, health, plan, quota windows, reset credits, proxy, pool role và actions:
Refresh quota, Refresh token, Reset quota OpenAI, Enable/Disable. Secret không
được render.

## Migration và backward compatibility

Migration additive, không sửa ngầm ý nghĩa cột cũ:

- quota snapshot/reset-credit JSONB có version và timestamp;
- account-pool policy và session binding;
- reset request idempotency/audit record;
- usage indexes cho account/model/time.

Dữ liệu credential hiện có vẫn chạy với direct proxy và pool mặc định một account.
API cũ `/credentials`, `/oauth/{id}/refresh`, `/v1/*` không đổi schema; endpoint
mới chỉ mở sau khi migration và capability checks hoàn tất.

## Security và vận hành

- Admin route yêu cầu private session + CSRF; public route chỉ nhận gateway key.
- Refresh/reset không cho phép user customer tự gọi.
- Không log token, Kiot key, client key, raw upstream body hoặc cookie.
- Timeout toàn request, timeout proxy lease và retry budget bị giới hạn.
- Không bật scheduler tự động trong startup public/admin; worker được khởi động
  tường minh.
- Không chạy reset, refresh quota, migration production hoặc test account thật
  trong CI; dùng fixture response đã redact.

## Kiểm thử chấp nhận

1. OpenAI `/v1/responses` JSON/SSE đi qua Codex OAuth binding và trả usage đúng.
2. Account pool random/priority/single, session affinity và cooldown có test.
3. Upstream 429 trước generation retry đúng một lần, partial stream không retry.
4. Usage fixture inclusive-cache và exclusive-cache cho kết quả weighted giống
   nhau; cached không double-count; 100m quota không overflow.
5. Reset-credit GET/POST có test UUID idempotency, no-credit, consume-success,
   refresh-failed và secret redaction.
6. Direct/fixed/pool/KiotProxy có test lease, rotation, release và error mapping.
7. Admin account/pool/quota UI không render token/secret; audit có action/result.
8. Existing Python 3.10/3.14 suites, Ruff, browser fixture và migration checks
   đều pass.

## Không thuộc phạm vi bản đầu

- Sao chép Cockpit sidecar hoặc desktop profile takeover.
- Hosted multi-tenant service không có admin boundary.
- WebSocket Codex hoặc image pool trước khi capability/test contract hoàn tất.
- Reset quota OpenAI không có reset-credit upstream.
- Xóa ledger để “reset” quota khách.
