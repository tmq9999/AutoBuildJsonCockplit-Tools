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
   Key mới mặc định không có quota/model để gọi.
5. Dùng **Chạy thử API** với client key; request chịu đúng quyền và quota.
   OAuth import cần refresh/kiểm chứng trước khi đưa vào tuyến hoạt động.

## Quota và chi phí

```text
Token quy đổi = input_tokens × hệ số input + output_tokens × hệ số output
```

1.000 input × 1 + 500 output × 3 = **2.500 token quy đổi**.
UI dùng token; private admin API dùng micro-unit:
`1 token quy đổi = 1.000.000 micro-unit`, truyền bằng chuỗi thập phân để tránh JS
làm tròn. Usage thực và chi phí mua vào được lưu riêng.

- Giữ quota trước dispatch, quyết toán actual usage một lần, trả phần giữ thừa.
- Thiếu usage/timeout không rõ kết quả → `usage_pending`; không giả usage=0 hay
  tự hoàn hết. Admin đối soát bằng evidence và audit adjustment.
- Cửa sổ ngày/tháng theo UTC lúc nhận request; `null` = không giới hạn, `0` = hết.
- Không tự replay generation không rõ kết quả hoặc khi đã bắt đầu stream.

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
```

Test binaries cần `initdb`/`pg_ctl`; tests tạo cluster tạm có password. Python
integration và gateway browser tests cũng có thể dùng `AUTOBUILD_TEST_DATABASE_URL`: database test-only
loopback, port tường minh, tên `abgw_test_<32 ký tự hex>`, role được tạo/drop DB con.
Không dùng database thật. Chrome dùng `/usr/bin/google-chrome` hoặc `CHROME_PATH`.
Test không gọi account/model thật; provider I/O dùng dữ liệu tổng hợp.

Checkpoint local: **450 Python tests trên cả 3.10/3.14, 12 Node tests, hai Chrome E2E**, lint/build qua.
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
hứa đầy đủ mọi beta feature hay phiên bản Codex CLI/Claude Code. Chưa có payment,
embeddings, tạo ảnh/audio/video, realtime/WebSocket hoặc chạy shell/tool thay khách.
Codex OAuth không nhận mọi tham số Platform API; đọc matrix trước khi dùng.

Resale phải phù hợp điều khoản của từng provider. Repo chưa cấp giấy phép phân
phối chung; không suy quyền thương mại từ project tham khảo.

Tài liệu: [vận hành/backup/restore](docs/gateway-operations.md) ·
[CHANGELOG](CHANGELOG.md) · [trạng thái](docs/implementation-status.md) ·
[quyết định kỹ thuật](docs/gateway-implementation-decisions.md).
