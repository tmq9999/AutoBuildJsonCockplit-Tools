# AutoBuildJsonCockplit-Tools

Gateway API đa chuẩn và công cụ OAuth HTTP, kèm giao diện quản trị tiếng Việt.
Admin quản lý khách hàng, API key, model, quota, hệ số token, provider bên ngoài
và proxy; khách gọi model qua key riêng, không nhận credential upstream.

Code gateway được xây dựng riêng, tham khảo kiến trúc FreeLLMAPI, Cockpit Tools
và 9router; không đóng gói source/sidecar của các project này.

> Đã kiểm thử offline với PostgreSQL thật, SDK và Chrome. Chưa xác nhận live
> Codex inference, KiotProxy hoặc provider bên ngoài bằng credential thật cho
> gateway. Tương thích giao thức không tự cấp quyền resale dịch vụ.

## Chức năng

- **API key:** tạo, đổi secret, sửa tên/policy, khóa/thu hồi, hết hạn; secret chỉ
  hiện một lần. Một khách hàng có thể sở hữu nhiều key.
- **Model/quota:** allowlist model và giao thức, alias/mapping, quota tổng/ngày/
  tháng, RPM/concurrency, hệ số input/output riêng theo model/key.
- **Provider:** Base URL, chuẩn API/auth header, credential mã hóa, discovery có
  bước chọn trước khi công bố; routing ưu tiên, round-robin, cooldown và budget.
- **Proxy:** direct, proxy cố định, danh sách HTTP/HTTPS/SOCKS5 và KiotProxy
  (mỗi dòng một API key; vùng Bắc/Trung/Nam/ngẫu nhiên).
- **OAuth:** batch HTTP tuần tự/song song, PKCE/callback thủ công, import JSON,
  refresh token có khóa chống gửi trùng; lỗi riêng và `Phone number verify`.
- **Vận hành:** usage/audit, playground JSON, phục hồi request chưa quyết toán,
  snapshot mã hóa và restore vào database trống.

## Chuẩn API

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

Mở `http://127.0.0.1:8787/service/`. Admin token nằm trong
`data/local-config.json`; lấy trực tiếp trên máy, không gửi lên GitHub. Đây không
phải client API key hay token OpenAI.

Nếu server OAuth cũ đang chạy, không dùng cùng port/data directory:

```bash
export AUTOBUILD_DATA_DIR='data/gateway-admin'
.venv/bin/python -m autobuild_json.gateway admin --port 8789
```

Khi đó UI ở `http://127.0.0.1:8789/service/`, token ở
`data/gateway-admin/local-config.json`. Không chạy hai coordinator cùng data.

### Thiết lập qua UI

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
fallback direct. Proxy pool/list khác với account pool auto/random/single/priority/weight;
account selection và session affinity đều chịu policy và account health.

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
- External adapters giữ cooldown toàn provider. Codex đánh cooldown account bị 429 và
  chỉ thử tối đa một account khác cùng provider khi fresh/unpinned và policy cho phép;
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

Bản đầu hỗ trợ text/basic tools và streaming theo capability đã kiểm thử; không
hứa đầy đủ mọi beta feature hay phiên bản Codex CLI/Claude Code. Codex subset hiện
không hỗ trợ compact, images, realtime/WebSocket, profile takeover, shell/tool chạy
thay khách, hay toàn bộ tham số Platform API. Chưa có payment, embeddings, tạo
ảnh/audio/video.
Codex OAuth không nhận mọi tham số Platform API; đọc matrix trước khi dùng.

Resale phải phù hợp điều khoản của từng provider. Repo chưa cấp giấy phép phân
phối chung; không suy quyền thương mại từ project tham khảo.

Tài liệu: [vận hành/backup/restore](docs/gateway-operations.md) ·
[CHANGELOG](CHANGELOG.md) · [trạng thái](docs/implementation-status.md) ·
[quyết định kỹ thuật](docs/gateway-implementation-decisions.md).
