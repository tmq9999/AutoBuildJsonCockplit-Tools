# AutoBuildJsonCockplit-Tools

Gateway API đa chuẩn và công cụ OAuth HTTP, kèm giao diện quản trị tiếng Việt.
Admin quản lý khách hàng, API key, model, quota, hệ số token, provider bên ngoài
và proxy; khách gọi model qua key riêng, không nhận credential upstream.

Code gateway được xây dựng riêng, tham khảo kiến trúc FreeLLMAPI, Cockpit Tools
và 9router; không đóng gói source/sidecar của các project này.

> Đã kiểm thử offline với PostgreSQL thật, SDK và Chrome. Ngày 2026-09-26 đã
> xác nhận live `POST /v1/responses` qua direct, dùng một tài khoản OAuth đã import:
> `gpt-6-astra`, HTTP 200, nội dung `OK`, input 10 / cached 0 / output 5;
> ledger quyết toán đúng 15 token quy đổi ở hệ số 1. Đây không phải xác nhận
> toàn bộ tài khoản/model, streaming client, image hay compact live. Follow-up
> đã xác nhận một lượt WebSocket thật: 12 input / 7 output, quyết toán đúng
> 26 token quy đổi tại input ×1/output ×2. Chi tiết: [live verification](docs/live-verification-2026-09-26.md).
> KiotProxy và provider bên ngoài chưa được xác nhận live. Tương thích giao thức
> không tự cấp quyền resale dịch vụ.

## Chức năng

- **API key:** tạo, đổi secret, sửa tên/policy, khóa/thu hồi, hết hạn; secret chỉ
  hiện một lần. Một khách hàng có thể sở hữu nhiều key.
- **Model/quota:** allowlist model và giao thức, alias/mapping, quota tổng/ngày/
  tháng, RPM/concurrency, hệ số input/output riêng theo model/key.
- **Provider:** Base URL, chuẩn API/auth header, credential mã hóa, discovery có
  bước chọn trước khi công bố; routing ưu tiên, round-robin, cooldown và budget.
  Codex pool có thể chọn toàn bộ account đã liên kết (`all_accounts`), xoay con
  trỏ qua các request và thử tối đa 8 account khác nhau cho một request nếu lỗi
  được xác định là trước generation (giống giới hạn retry của Cockpit).
- **Proxy:** direct, proxy cố định, danh sách HTTP/HTTPS/SOCKS5 và KiotProxy
  (mỗi dòng một API key; vùng Bắc/Trung/Nam/ngẫu nhiên).
- **OAuth:** batch HTTP tuần tự/song song, PKCE/callback thủ công, import JSON,
  refresh token có khóa chống gửi trùng; lỗi riêng và `Phone number verify`.
- **Vận hành:** usage/audit, playground JSON, phục hồi request chưa quyết toán,
  snapshot mã hóa và restore vào database trống.

## Chuẩn API

Codex OAuth hiện cung cấp các đường tương thích Responses/Images:

- `GET /v1/models`, `/models`, `/backend-api/codex/models` — model được phép bởi
  API key; thêm `?client_version=0.1.0` để nhận metadata theo dạng Codex `models`.
  Admin xem catalog tham khảo ở `/api/service/codex-models` và công bố qua UI.
  Các model GPT-6/GPT-5.6/GPT-5.5 hiện đã được công bố và bind vào pool động:
  `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`,
  `gpt-5.6-luna`, `gpt-5.5`; mỗi model có 226 bindings. Model mới trong catalog
  vẫn cần publish/bind trước khi gọi; import tài khoản không tự làm việc đó.
  Catalog không chứng minh quyền dùng model; `model_not_supported` nghĩa là
  upstream từ chối model trên tài khoản được chọn. Không đổi model ngầm;
  request mới không ghim session có thể chuyển sang tài khoản tiếp theo trong
  giới hạn pool, tối đa 8 tài khoản/request. Binding/model bị từ chối được
  cooldown cục bộ 5 phút, không làm tắt các model khác của tài khoản.
- `POST /v1/responses` và `POST /backend-api/codex/responses` — Responses SSE.
- `POST /v1/responses/compact` và alias backend — compaction response có usage.
- `POST /v1/images/generations`, `POST /v1/images/edits` — JSON hoặc multipart
  edit, trả `{created,data:[{b64_json|url,revised_prompt}]}`.
- `wss://<gateway>/v1/responses` — Responses WebSocket beta; client gửi
  `response.create`, gateway giữ quota/proxy/account routing như HTTP.

Image capability vẫn phụ thuộc binding/account thực tế; catalog không tự cấp quyền
cho tài khoản không có binding. WebSocket upstream dùng header
`OpenAI-Beta: responses_websockets=2026-02-06`.

WebSocket hiện xử lý tuần tự: chờ terminal event trước khi gửi lượt tiếp theo.
`stream_id` hợp lệ được trả lại ở cả event và lỗi, nhưng chưa chạy nhiều lane
đồng thời; `generate:false`/warmup chưa hỗ trợ. Giới hạn kết nối 30 phút, idle
5 phút, message 16 MiB. Mỗi lượt mở một kết nối upstream riêng: chưa xác nhận
continuation dựa trên cache của cùng socket ở upstream; gửi đủ context khi cần.
Ngoài kiểm thử hồi quy, một lượt WebSocket đã được xác nhận với upstream thật;
chưa xác nhận mọi chế độ Codex CLI hoặc continuation dùng lại cùng socket.

| Client | Endpoint chính | Xác thực |
|---|---|---|
| OpenAI | `/v1/chat/completions`, `/v1/responses`, `/v1/models` | Bearer |
| Anthropic | `/v1/messages`, `/v1/messages/count_tokens` | `x-api-key` hoặc Bearer; `anthropic-version` |
| Gemini | `/v1beta/models/{model}:generateContent`, `:streamGenerateContent`, `:countTokens` | `x-goog-api-key` hoặc Bearer |
| Ollama | `/api/chat`, `/api/generate`, `/api/tags`, `/api/show` | Bearer bắt buộc |

Streaming dùng SSE hoặc NDJSON theo chuẩn client. Inbound protocol độc lập với
provider; chẳng hạn Messages client có thể dùng model OpenAI-compatible.
Tính năng không biểu diễn được trên tuyến được chọn phải trả lỗi, không giả hỗ trợ.
Xem [ma trận tương thích](docs/gateway-compatibility.md).

## Yêu cầu và kiến trúc

Python **3.10+**, Git; PostgreSQL **17** cho gateway. Node/Chrome chỉ cần để phát
triển hoặc kiểm thử UI, không cần để chạy ứng dụng đã đóng gói.

| Thành phần | Địa chỉ mặc định | Vai trò |
|---|---|---|
| Private admin + OAuth | `http://127.0.0.1:8787` | Workbench `/`, quản trị `/service/` |
| Gateway | `http://127.0.0.1:8788` | API client, không mount quản trị |
| Maintenance | Không mở port | Đối soát request và dọn state hết hạn |

PostgreSQL lưu policy, quota ledger, token/credential mã hóa và lease dùng chung.
Master key ở ngoài database. Export OAuth cũ trong `data/` không tự được mã hóa
bởi vault mới; bảo vệ riêng như mật khẩu.

## Cài đặt gateway

```bash
git clone git@github.com:tmq9999/AutoBuildJsonCockplit-Tools.git
cd AutoBuildJsonCockplit-Tools
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[service]'
```

Repo private: GitHub account/SSH key phải được cấp quyền. Windows dùng
`.venv\Scripts\python.exe`. Linux thiếu `ensurepip` cần gói venv tương ứng;
không ghi đè môi trường Python hệ thống.

### Database và keyring

Chuẩn bị PostgreSQL với database/role riêng. Các giá trị sau chỉ là ví dụ; thay
trên máy của bạn, không commit cấu hình thật hoặc ghi password vào history dùng chung.

```bash
export AUTOBUILD_GATEWAY_DATABASE_URL='postgresql+psycopg://USER:URL_ENCODED_PASSWORD@127.0.0.1:5432/gateway'
export AUTOBUILD_GATEWAY_MASTER_KEY_FILE='/absolute/private/keyring.json'
.venv/bin/python -m autobuild_json.gateway init-keyring --key-file /absolute/private/keyring.json
.venv/bin/python -m autobuild_json.gateway migrate
```

Thư mục chứa keyring phải tồn tại và có quyền riêng tư. Lệnh khởi tạo không ghi
đè keyring có sẵn. Sao lưu keyring tách biệt với database. Gateway đọc environment
variables; **không tự nạp file `.env`** cho service settings.

### Chạy dịch vụ

Ba terminal/process cần cùng biến môi trường ở trên:

```bash
# Terminal 1: API khách
.venv/bin/python -m autobuild_json.gateway serve
```

```bash
# Terminal 2: UI/API quản trị private
.venv/bin/python -m autobuild_json.gateway admin
```

```bash
# Terminal 3: phục hồi/đối soát
.venv/bin/python -m autobuild_json.gateway maintenance
```

Để thử cục bộ với PostgreSQL đã giải nén/cài sẵn (không dùng database production):

```bash
.venv/bin/python scripts/local_gateway.py --postgres-bin /absolute/path/to/postgres/bin
```

Runner giữ database, keyring và admin config trong `data/local-gateway/` (private,
không commit), chỉ mở loopback 55433/8787/8788 và không tạo model/key khách giả.
Có thể thêm `--import-json /absolute/path/to/oauth-export.json` để nhập mảng JSON
OAuth đã có, tối đa 1000 records/32 MiB; không tự login hoặc refresh token. Dừng
bằng Ctrl+C sẽ dừng đúng các process do runner tạo, giữ nguyên dữ liệu cho lần chạy sau.
Nếu dùng bộ PostgreSQL riêng cần thiết lập thư viện của bộ đó trước khi chạy.

Trên Linux có systemd user, có thể chạy runner dưới service nền để việc đóng
terminal không làm mất hai listener (dừng runner foreground trước, không chạy
hai runner cùng data directory):

```bash
systemd-run --user --unit=autobuild-local-gateway --working-directory="$PWD" \
  --property=KillMode=mixed --property=TimeoutStopSec=90 \
  --setenv=LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}" \
  "$PWD/.venv/bin/python" "$PWD/scripts/local_gateway.py" \
  --postgres-bin /absolute/path/to/postgres/bin
systemctl --user status autobuild-local-gateway
# Restart hoặc dừng có kiểm soát, không xóa database:
systemctl --user restart autobuild-local-gateway
systemctl --user stop autobuild-local-gateway
```

Đây là transient user service, không phải cấu hình tự khởi động sau reboot.
`KillMode=mixed` cho runner đóng API và PostgreSQL trước khi systemd dọn process.

Mở `http://127.0.0.1:8787/service/`. Với runner local, admin token nằm trong
`data/local-gateway/admin-data/local-config.json`; chạy admin riêng với cấu hình
mặc định thì ở `data/local-config.json`. Lấy trực tiếp trên máy, không gửi lên GitHub. Đây không
phải client API key hay token OpenAI.

Nếu server OAuth cũ đang chạy, không dùng cùng port/data directory:

```bash
export AUTOBUILD_DATA_DIR='data/gateway-admin'
.venv/bin/python -m autobuild_json.gateway admin --port 8789
```

Khi đó UI ở `http://127.0.0.1:8789/service/`, token ở
`data/gateway-admin/local-config.json`. Không chạy hai coordinator cùng data.

### Thiết lập qua UI

Trang `/service/` mở workspace **Codex** với năm tab theo cấu trúc dịch vụ API
trong source Cockpit, dùng frontend riêng kết nối backend của project này:

- **Tổng quan dịch vụ:** Base URL, tình trạng đã lưu, proxy đầu ra và đường dẫn API.
  “Đã cấu hình” không có nghĩa là đã kiểm tra kết nối provider.
- **Khóa máy khách:** tạo/sửa/bật/tắt/đổi/xóa key ngay trong tab. Key đầy đủ chỉ
  hiện một lần khi tạo/đổi; đóng hộp thoại sẽ xóa secret khỏi giao diện.
- **Nhóm tài khoản:** email đầy đủ, quota/reset credit và form định tuyến theo
  model ở cột bên phải. Danh sách dùng phân trang server-side 24/48/96 dòng, có
  đầu/trước/số trang/sau/cuối và nhảy trang; tìm email/lọc trạng thái áp dụng
  toàn bộ catalog, không tải 1000 tài khoản vào một DOM. **Thêm tài khoản** mở
  hộp thoại nhập nhiều JSON hoặc mảng JSON; đóng/mở lại không làm mất draft.
  Đây là nhập OAuth đã có, không tự đăng nhập.
  Nút **Refresh quota tất cả** tạo một job server-side áp dụng cho toàn bộ tài
  khoản đang bật, độc lập với trang/bộ lọc hiện tại. UI hiển thị tiến độ thành
  công/lỗi/bỏ qua, tối đa hai lượt đồng thời, có nút dừng, tự ngắt khi upstream
  rate-limit và không chạy lại khi mất kết nối. Phần **Tự động refresh (phút)**
  hỗ trợ tắt (`0`), 2/5/10/15 hoặc số tùy chỉnh 1–999 phút; lịch được lưu trong
  PostgreSQL và worker tiếp tục chạy khi đóng trình duyệt. Mặc định luôn tắt.
- **Mô hình và năng lực:** chọn model từ catalog, xem trạng thái xuất bản, hệ số
  và khả năng adapter. Chưa xuất bản thì chưa xuất hiện trong `/v1/models`.
- **GPT-5.6 Reserve (`gpt-reserve`):** catalog và API allowlist hỗ trợ model này
  như một model riêng. Tài khoản chỉ được chọn khi quota upstream lưu đủ cả ba
  điều kiện: quota chính `allowed=false`, banner `luna_reserve`, và mục
  `additional_rate_limits` của `gpt-reserve` có `allowed=true`. Quota Reserve
  hiển thị riêng trên từng tài khoản; snapshot thiếu/cũ/lỗi bị coi là chưa rõ,
  không tự chuyển sang Luna/Astra và không bịa phần trăm còn lại.
- **Thống kê và nhật ký:** chọn 24 giờ/7 ngày/30 ngày hoặc khoảng UTC. Tổng
  requests/input/cache/output/reasoning/quota được tính phía server trên **toàn
  khoảng thời gian và bộ lọc**, không phụ thuộc trang log. Biểu đồ attempts
  vẫn ghi rõ phạm vi trang hiện tại. Tổng quan dịch vụ hiển thị số tích lũy toàn
  gateway, tự cập nhật mỗi 10 giây khi mở tab.

Không có nút Sidecar/start-stop giả hoặc trường định tuyến không được backend
hỗ trợ. Các màn quản trị khách hàng/provider/proxy vẫn ở sidebar. Bộ kiểm tra
layout và thao tác key mới: `node tests/ui/codex-layout-smoke.mjs` (cần cùng
cấu hình PostgreSQL fixture như các browser smoke khác).

**Chạy thử API thật:** chọn model đã xuất bản, Responses/Chat và mức thinking,
nhập API key được cấp model rồi bấm **Gửi request thật**. Có thể chỉnh JSON để
dùng model prefix/alias của key. Màn này dùng cùng engine/proxy/pool và tính quota
như API công khai, không phải response giả lập.

Thinking Codex: Responses dùng `reasoning: {"effort":"high"}`, Chat dùng
`reasoning_effort: "high"`. Messages nhận `thinking: {"type":"enabled",
"budget_tokens":1024}` (với `max_tokens` lớn hơn budget), hoặc `adaptive` cùng
`output_config.effort`. Khi sang Codex, budget được đổi sang mức effort, **không
phải giới hạn token cứng**. `ultra` bị endpoint Codex thực tế từ chối; gateway
không quảng bá mức này hoặc âm thầm đổi thành `max`.
Gọi `/v1/messages` phải có header `anthropic-version: 2023-06-01`.

Usage lấy từ upstream sau khi request hoàn tất: tổng = input + output; cached
là phần con của input, reasoning là phần con của output. Request chưa có usage
được thống kê riêng, không bịa số token hay xóa lịch sử chờ đối soát. Kết quả
gọi thật và giới hạn đã xác minh: [live verification](docs/live-verification-2026-09-26.md).
Lọc báo cáo theo tài khoản chỉ gán token/charge cho tài khoản phục vụ thực tế;
attempt bị từ chối trước retry không nhận usage của tài khoản thay thế.

Kiểm chứng trình duyệt với upstream thật (gateway local đang chạy, có tài khoản
được dùng `gpt-6-astra`): `AUTOBUILD_LIVE_VERIFY=1 node tests/ui/codex-playground-live.mjs`.
Lệnh gửi một request thật, tiêu quota upstream, tạo cấu hình thử tạm rồi thu hồi
key/tắt cấu hình; lịch sử usage và audit được giữ. Không nằm trong suite offline.

Kiểm chứng HTTP tools/usage đa chuẩn: `AUTOBUILD_LIVE_VERIFY=1 node tests/ui/gateway-review-live.mjs`.
Kiểm chứng WebSocket thật: `AUTOBUILD_LIVE_VERIFY=1 .venv/bin/python tests/gateway/live_websocket.py`.
Cả hai tạo key thử ngắn hạn và thu hồi sau khi chạy; request không rõ usage vẫn
giữ trạng thái chờ đối soát, không giả token bằng 0.

API quản trị refresh (private admin + CSRF): `GET /api/service/codex-service/quota-refresh`
đọc trạng thái; `POST /api/service/codex-service/quota-refresh` với
`request_id` xếp hàng refresh tất cả; `POST .../quota-refresh/stop` dừng job;
`PUT .../quota-refresh/settings` với `{version, interval_minutes}` cập nhật lịch
theo CAS. Migration `0014_codex_quota_refresh` tạo lịch mặc định tắt và lịch sử
job; backup/restore giữ các bảng này và luôn tắt lịch sau restore.

1. Thêm **Provider**: adapter, Base URL với prefix phù hợp (`/v1`, `/v1beta` hoặc
   `/api`), auth mode, timeout, RPM/concurrency và proxy profile nếu cần.
2. Thêm **Credential** cho provider. Secret upstream không hiển thị lại.
3. Thêm **Model** public và **Model mapping** tới upstream model/credential;
   khai báo input/output bound đúng thực tế, không tùy ý đặt thấp.
4. Thêm **Khách hàng**, tạo **API key**, cấp model/giao thức/quota.
   Kiểm tra model/giao thức đã chọn và nhập `100000000` để cấp 100 triệu token quy đổi.
   Quota để trống là không giới hạn; `0` là hết quota. API tạo key với policy trống
   mặc định quota bằng 0 và chưa cấp model/giao thức.
5. Dùng **Chạy thử API** với client key; request chịu đúng quyền và quota.
   OAuth import cần refresh/kiểm chứng trước khi đưa vào tuyến hoạt động.

## Quota và chi phí

```text
uncached_input = input_tokens - cached_read - cached_write
Token quy đổi = uncached_input × hệ số input + cached_read × hệ số cache_read
             + cached_write × hệ số cache_write + output_tokens × hệ số output
```

Không có cache: 1.000 input × 1 + 500 output × 3 = **2.500 token quy đổi**.
Reasoning đã nằm trong output, không cộng lần hai.
UI dùng token; private admin API dùng micro-unit:
`1 token quy đổi = 1.000.000 micro-unit`, truyền bằng chuỗi thập phân để tránh JS
làm tròn. Usage thực và chi phí mua vào được lưu riêng.

- Giữ quota trước dispatch, quyết toán actual usage một lần, trả phần giữ thừa.
- Thiếu usage/timeout không rõ kết quả → `usage_pending`; không giả usage=0 hay
  tự hoàn hết. Admin đối soát bằng evidence và audit adjustment.
- Cửa sổ ngày/tháng theo UTC lúc nhận request; `null` = không giới hạn, `0` = hết.
- Không tự replay generation không rõ kết quả hoặc khi đã bắt đầu stream.

### Codex API Service

Codex OAuth hiện được hỗ trợ trong một HTTP subset đã kiểm thử qua
`/v1/chat/completions` và `/v1/responses`, gồm text, function tools và SSE streaming.
Native Responses reasoning summary được giữ cùng encrypted replay payload dạng opaque;
không lộ chain-of-thought và không chuyển reasoning sang giao thức không hỗ trợ.
Private admin service (`/api/service`) cung cấp account views với email đầy đủ trong
admin private, storage-only
usage/credit snapshots, explicit refresh, UUID-idempotent reset credits, pool/CAS,
usage reports, key grants và capabilities. Đây là API nội bộ reference-observed,
không phải cam kết tương thích đầy đủ Codex CLI hay OpenAI Platform.

Quota có ba lớp tách biệt: weighted-token quota của customer key, quota window phần
trăm do Codex account cấp, và reset credits do upstream cấp. Không dùng phần trăm/credit
upstream để tự động thay đổi customer ledger; key grant chỉ tăng total limit và giữ
nguyên spent/held. Rate cache đọc/cache ghi kế thừa input khi để trống, còn giá trị
zero là miễn phí rõ ràng.

Thứ tự proxy của account là credential override, kế đến provider profile, rồi direct;
fixed/list/KiotProxy chỉ được dùng khi profile tương ứng được cấu hình, không tự
fallback direct. Proxy pool/list khác với account pool auto/round_robin/random/single/priority/weight;
account selection và session affinity đều chịu policy và account health.

Trong **Tùy chọn định tuyến**, chọn `round_robin` và bật **Toàn bộ tài khoản đã
liên kết model** để xoay tuần tự toàn pool động. `all_accounts=false` vẫn dùng
danh sách thành viên thủ công; danh sách rỗng ở chế độ này là tạm dừng, không
tự mở toàn bộ tài khoản. `retry_limit=0..7` là số lần chuyển tài khoản tối đa
sau lượt đầu (mặc định 7 cho policy mới; policy đã lưu giữ nguyên giá trị).
Tài khoản unverified/reauth_required/refresh_uncertain, disabled, cooldown hoặc
đã biết hết quota không được phục vụ. Dùng Refresh quota/OAuth để xác minh lại;
pool không tự đặt lại quota hay ép health thành active. Lỗi refresh trước
inference bỏ qua tài khoản đó, vẫn giữ nguyên trạng thái cần xử lý.

Kiểm thử live 2026-09-26 sau khi bỏ pin: bốn request `gpt-6-astra` đều completed
qua bốn tài khoản khác nhau; một request qua 3 attempts và một qua 2 attempts
sau rejection. Ledger chỉ quyết toán một lần/request: 19, 20, 20, 20 token
quy đổi. Pool triển khai bao gồm 226 bindings, 51 active / 155 unverified /
20 refresh_uncertain tại thời điểm kiểm thử; không phải 226 tài khoản đã chạy thành công.

Responses native hỗ trợ `reasoning` (effort/summary/context),
`include: ["reasoning.encrypted_content"]`, `text` (verbosity/format),
`prompt_cache_key` và function tool `strict`. Các option native không biểu diễn được
trên wire API khác bị từ chối trước dispatch. Codex bỏ `max_output_tokens`,
`temperature`, `top_p` theo cách chuẩn hóa của Cockpit: các giá trị này **không phải
giới hạn generation được đảm bảo**; quota vẫn giữ trước theo output bound đã cấu hình.

Overview Codex cho phép đặt proxy mặc định cho provider (direct/fixed/list/pool/KiotProxy);
thay đổi dùng optimistic version và không tự ghi lại khi gặp 409. `GET/PUT
/api/service/codex-service/proxy` chỉ là private control-plane, không trả endpoint,
API key proxy hoặc metadata proxy trong response của khách hàng. Credential override
luôn được ưu tiên hơn cấu hình mặc định.

Reset phải được xác nhận tường minh bằng request UUID, credits version và acknowledgement.
Replay cùng payload chỉ đọc receipt đã lưu và không POST upstream lần hai. `unknown`
vẫn khóa sau refresh và chỉ được giải quyết bằng explicit evidence resolution.
`succeeded_refresh_failed` đã có receipt upstream thành công: explicit admin refresh
đủ cả usage + credits fresh, không lỗi có thể hoàn tất `succeeded` qua fence generation,
stamp và snapshot sau receipt; partial/stale refresh vẫn khóa. UI chỉ mở khóa từ
bằng chứng operation của server, không suy ra từ phần trăm hay `active_reset=null`.
Nếu token generation hoặc cấu hình account/proxy đã thay đổi sau receipt, refresh
không tự bỏ khóa cũ; admin dùng đối soát bằng bằng chứng với version hiện tại.
Account đã biết hết quota vẫn bị loại khỏi pool khi snapshot quá hạn hoặc refresh
lỗi, đến khi có snapshot mới xác nhận còn quota.
HTTP 4xx từ chối consume chỉ gỡ khóa lạc quan sau khi đọc lại trạng thái xác thực thành công;
timeout/cancel/5xx vẫn fail-closed, không tự gửi lại. Credit phải fresh (120 giây), có count > 0, đúng version, account active,
không cooldown/active reset. Khi upstream/provider thật chưa được
cấp phép, chỉ dùng synthetic HTTP và disposable PostgreSQL fixtures.

UI quản trị Codex Accounts hiện theo visual draft v6 đã được duyệt: full private emails,
exact decimal meters, actual window durations, explicit reset/grant confirmation và
evidence resolution. Provider Catalog UI vẫn là work riêng đang pending, không được coi
là hoàn tất.

Ví dụ request (model phải được publish và cấp cho client key; không chạy các ví dụ
bằng credential thật trong kiểm thử):

```http
POST /v1/responses
Authorization: Bearer <CLIENT_KEY>
Content-Type: application/json

{"model":"codex-public","input":"Hello","stream":true}
```

Private admin mutations dùng cookie session, đúng Origin và `X-CSRF-Token`, không dùng
client key; public listener trả 404 cho `/api/service`. Ví dụ body sau khi đọc snapshot:

```text
POST /api/service/oauth-accounts/<UUID>/quota/refresh
{}
POST /api/service/oauth-accounts/<UUID>/reset-credits/consume
{"request_id":"<NEW_CONFIRMATION_UUID>","credits_version":7,"acknowledge":true}
POST /api/service/reset-requests/<CONFIRMATION_UUID>/resolve
{"version":3,"outcome":"confirmed_not_applied","reason":"Provider evidence reviewed"}
POST /api/service/keys/<KEY_UUID>/quota-adjust
{"request_id":"<NEW_GRANT_UUID>","version":2,"amount_micro":"1085000000","reason":"Restore consumed allocation"}
```

Versions là ví dụ, phải đọc lại server; giữ nguyên UUID/body khi reconcile receipt,
không tự retry POST. Grant 1.085 token sau 1.085 spent nâng total 100m thành 100.001.085,
giữ spent/held và khôi phục available 100m; không reset quota upstream hay day/month.

### Rate limit và cooldown

- Upstream từ chối bằng HTTP 429 → gateway trả **429**, không gộp thành lỗi 502.
  Áp dụng cho các adapter generation và native Anthropic/Gemini token counting;
  body lỗi vẫn theo chuẩn API của client, không lộ nội dung/header bí mật upstream.
- `Retry-After` nhận số giây hoặc HTTP-date, làm tròn lên và giới hạn 24 giờ.
  Giá trị `0` được giữ; header thiếu/sai không được giả thành thời gian upstream hứa.
- External adapters chỉ thử tối đa một provider dự phòng sau 429 trước generation. Khi mọi provider
  ứng viên đều bị giới hạn và đều có thời gian chờ hợp lệ, trả thời điểm sớm nhất
  có thể thử lại. Nếu còn provider chưa thử hoặc có hint không rõ, bỏ header đó.
- Cooldown dùng PostgreSQL, không bị worker khác rút ngắn; kiểm tra lại trước
  dispatch. Nếu các tuyến đủ điều kiện đều đang cooldown, request mới nhận 429
  và thời gian chờ còn lại theo lịch **cục bộ**, không gọi upstream.
- Tính thời gian chờ từ lúc nhận rejection, không bắt đầu lại sau cleanup.
  Hint cuối xét cả tuyến đã cooldown trước đó và cooldown dài hơn từ worker khác.
  Responses continuation kiểm tra handle và giữ đúng cooldown của binding gốc.
- External adapters giữ cooldown toàn provider. Codex đánh cooldown account bị 429,
  model entitlement failure theo binding/model và thử tối đa 8 account khác nhau
  cùng provider khi fresh/unpinned và policy cho phép;
  session/continuation pin không fallback. Không xoay IP để né giới hạn. Thiếu hint
  dùng cooldown cục bộ 60 giây; timeout, 5xx không rõ kết quả và stream đã mở không
  tự replay. Request bị từ chối rõ ràng được trả phần quota/budget đã giữ.

## Proxy và KiotProxy

Proxy tĩnh nhận URL, `host:port`, `host:port:user:pass` hoặc
`http|host|port|user|pass`. Danh sách mỗi dòng một mục. Kiot mode nhận **API key**
mỗi dòng, không phải URL; hỗ trợ `random`, `bac`, `trung`, `nam`, HTTP/SOCKS5.

Giữ proxy trong mỗi operation, tôn trọng cooldown/TTL và giới hạn dùng key đồng
thời. Không đổi IP để vượt giới hạn upstream; proxy lỗi không tự fallback direct.
Kiot `/out` chỉ gọi khi admin chủ động giải phóng key idle. Không chia sẻ Kiot key
với tool khác nếu cần bảo đảm không bị đổi IP bên ngoài.

## OAuth-only, không cần PostgreSQL

```bash
.venv/bin/python -m pip install -e '.[proxy]'
.venv/bin/python scripts/setup_checklive.py
.venv/bin/python -m autobuild_json
```

Input `Email|Password|2FA_SECRET`, mỗi dòng một tài khoản. Dòng thiếu/sai thành
phần bị lọc; không sửa file account gốc. TOTP không thay thế yêu cầu xác minh
email/phone của provider.

CheckLive chỉ cần cho batch HTTP email/password/TOTP; gateway API-key và import/
OAuth thủ công không cần nó. Dependency được pin, không đóng gói vào repo/build.
Xem [hướng dẫn OAuth](docs/oauth-workbench.md) và [THIRD_PARTY.md](THIRD_PARTY.md).

## Kiểm thử

```bash
.venv/bin/python -m pip install -e '.[dev,service,gateway-test]' -r requirements/gateway-sdk-tests.lock
.venv/bin/python scripts/setup_checklive.py
export AUTOBUILD_TEST_POSTGRES_BIN='/absolute/path/to/postgresql/bin'
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m compileall -q autobuild_json
.venv/bin/python -m build
npm ci
npm run test:ui
npm run test:browser
npm run test:gateway-browser
node tests/ui/codex-browser-smoke.mjs
```

Test binaries cần `initdb`/`pg_ctl`; tests tạo cluster tạm có password. Python
integration và gateway browser tests cũng có thể dùng `AUTOBUILD_TEST_DATABASE_URL`: database test-only
loopback, port tường minh, tên `abgw_test_<32 ký tự hex>`, role được tạo/drop DB con.
Không dùng database thật. Chrome dùng `/usr/bin/google-chrome` hoặc `CHROME_PATH`.
Test không gọi account/model thật; provider I/O dùng dữ liệu tổng hợp.

Nếu dùng PostgreSQL 18 bundle ở checkout này, cần cả đường dẫn binary và thư viện
(các biến export chỉ có hiệu lực trong terminal hiện tại):

```bash
export AUTOBUILD_TEST_POSTGRES_BIN="$PWD/.deps/gateway-pg18/root/usr/lib/postgresql/18/bin"
export LD_LIBRARY_PATH="$PWD/.deps/gateway-pg18/root/usr/lib/x86_64-linux-gnu:$PWD/.deps/gateway-pg18/root/usr/lib/postgresql/18/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
.venv/bin/python -m pytest -q --tb=short
```

Checkpoint historical: 549 Python tests trên 3.10/3.14, 12 Node tests, hai Chrome E2E.
Task12 phải được đánh giá bằng output
của các lệnh chạy trên snapshot hiện tại. Codex verification dùng synthetic upstream/
PG test schemas, không dùng credential thật, provider/reset thật, production migration,
restart, deploy, push hay merge.
Task12 handoff checkpoint: 1312 tests trên mỗi Python3.10/3.14, 34 Node tests,
ba browser smokes, Ruff cả hai, compileall/diff-check qua. Final review tìm hai
lỗi về credit refresh thất bại và snapshot account hết quota quá hạn; xem kết quả
sửa và kiểm thử mới nhất trong [review cuối](docs/codex-final-review.md).
Số liệu checkpoint không thay thế kết quả chạy trên mã nguồn hiện tại. Xem chi tiết ở
[quyết định kỹ thuật](docs/gateway-implementation-decisions.md#task12-handoff--2026-09-25).
Xem [compatibility](docs/gateway-compatibility.md) và
[review resolution](docs/gateway-review-resolution.md). CI kiểm tra Python 3.10/3.14;
test local không thay thế trạng thái CI hiện tại.

## Bảo mật, triển khai và phạm vi

- Không commit `.env`, keyring, account/proxy-key list, `data/`, exports hoặc backup.
  Repo private vẫn không phải nơi lưu token.
- Chỉ public gateway sau TLS reverse proxy/hostname/trusted-proxy configuration;
  admin giữ loopback hoặc truy cập qua SSH/VPN. Không CORS wildcard.
- Egress chặn SSRF/direct DNS rebinding; proxy-side DNS cần chính sách proxy/
  firewall tin cậy. TLS verification luôn bật.
- Giữ master key cũ khi xoay encryption key; không đổi `client_keys` nếu muốn
  client key hiện tại còn hiệu lực.
- [Compose](deploy/gateway-compose.yml) là template local, không phải production
  deployment đã xác minh. Cài tool không tự public port.

Gateway hỗ trợ text/basic tools, compact, image generation/edit và Responses
WebSocket theo capability/binding đã cấu hình; không hứa đầy đủ mọi beta feature
hay phiên bản Codex CLI. Profile takeover, shell/tool chạy thay khách, payment,
embeddings, audio/video và toàn bộ tham số Platform API vẫn ngoài phạm vi.
Codex OAuth không nhận mọi tham số Platform API; đọc matrix trước khi dùng.

Resale phải phù hợp điều khoản của từng provider. Repo chưa cấp giấy phép phân
phối chung; không suy quyền thương mại từ project tham khảo.

Tài liệu: [vận hành/backup/restore](docs/gateway-operations.md) ·
[CHANGELOG](CHANGELOG.md) · [trạng thái](docs/implementation-status.md) ·
[quyết định kỹ thuật](docs/gateway-implementation-decisions.md).
