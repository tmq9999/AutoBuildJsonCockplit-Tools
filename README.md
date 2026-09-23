# AutoBuildJsonCockplit-Tools

Tool local dùng HTTP để đăng nhập tài khoản được phép sử dụng, thực hiện OAuth Codex
và xuất JSON tương thích mẫu của Auto-Oauth-Codex-9Router. Không cần chạy 9router.

Giao diện tiếng Việt đã ghép vào FastAPI tại `/`, theo bản Superdesign đã duyệt.
Đã thử đăng nhập thật sau bản sửa Sentinel: 1 tài khoản đã trả bộ token OAuth hợp lệ
và batch dừng ngay ở thành công đầu tiên. Endpoint đăng nhập nội bộ có thể thay đổi;
test giả lập không bảo đảm tất cả tài khoản sẽ đăng nhập thành công.

Luồng khởi tạo đã cập nhật theo capture: `GET /api/auth/providers` →
`GET /api/auth/csrf` → `POST /api/auth/signin/openai`. Không tải trang chủ trước
khi bắt đầu. `auth_session_logging_id` và `ext-oai-did` là hai UUID v4 mới cho mỗi
attempt; giữ cùng cookie jar qua ba request, không dùng cookie từ capture.
Signin có `prompt=login`, `screen_hint=login_or_signup`, `login_hint`, hai UUID;
form body được URL-encode đúng chuẩn. Request trang chủ vẫn có thể xuất hiện
**sau** MFA nếu callback của server redirect về `/`.

Đã kiểm chứng một lần OAuth thật với luồng này và nhận đủ ba token. Việc bỏ request
khởi tạo trang chủ không bảo đảm mọi request mạng khác sẽ hết timeout.

## Cài đặt

Python 3.10+ và Git. Đã kiểm thử trong môi trường Python 3.14 trên Linux.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python scripts/setup_checklive.py
.venv/bin/python -m autobuild_json
```

Windows dùng `.venv\Scripts\python.exe` thay `.venv/bin/python`. Nếu Linux thiếu
`ensurepip`, có thể cài gói `python3-venv` của hệ điều hành, hoặc dùng pip có sẵn để
cài vào venv: `python3 -m pip --python .venv/bin/python install pip`.

Script setup chỉ tạo checkout mới tại `.deps/Check-Account-ChatGPT`, cố định commit
`791beb350370c191bbe8ddbe0b4a3fa0072620d9`. Checkout có sẵn khác revision/bị sửa sẽ
bị từ chối, không reset. Có thể cấu hình đường dẫn khác bằng `CHECKLIVE_PATH`.
Source auth không có package manifest và không được pip-install như package.
Xem [THIRD_PARTY.md](THIRD_PARTY.md) trước khi phân phối lại source upstream.

API chạy tại `http://127.0.0.1:8787`. Chỉ một process được dùng cùng thư mục data.
Không chạy nhiều Uvicorn workers hoặc `--reload` khi có batch.
Đổi port: `.venv/bin/python -m autobuild_json --port 8788`.

## Dùng giao diện

1. Mở `http://127.0.0.1:8787`, nhập `admin_token` từ file local bên dưới.
2. Chọn file hoặc dán danh sách tài khoản; bấm **Kiểm tra dữ liệu** để xem số hợp lệ
   và các dòng bị lọc. Sửa dữ liệu sẽ yêu cầu kiểm tra lại.
3. Nhập proxy nếu cần; chọn **Tuần tự** hoặc **Song song**, số luồng và timeout.
4. Bấm **Bắt đầu**, theo dõi bảng trạng thái; **Dừng** ngừng lấy account mới.
5. Tải file theo nhóm ở phía trên bảng. Chọn **Batch đã lưu** để xem lại sau reload/restart.
6. Mở **OAuth thủ công** nếu muốn tạo link và tự dán callback từ trình duyệt.

Đăng xuất không dừng batch backend. Password/secret chỉ tồn tại trong ô nhập đến
khi Start được chấp nhận; không ghi localStorage/sessionStorage. Giao diện chỉ tải
assets nội bộ, không tải script/font/CDN bên thứ ba.

## Đăng nhập API local

Lần khởi động đầu tạo `data/local-config.json`, chứa `admin_token` và UUID cài đặt.
Mở file này **trên máy của bạn** để lấy token, không gửi token cho người khác.
Có thể dùng `AUTOBUILD_ADMIN_TOKEN` riêng trong `.env`; đừng commit `.env`.

1. `POST /api/session`, header `Origin: http://127.0.0.1:8787`, JSON
   `{"token":"YOUR_LOCAL_ADMIN_TOKEN"}`. Lưu cookie `autobuild_session` và
   `csrf_token` trả về.
2. Mọi POST/DELETE tiếp theo cần cookie, cùng Origin và `X-CSRF-Token`.
3. GET chỉ cần cookie; `GET /api/session` lấy lại CSRF cho phiên hiện tại.
4. `DELETE /api/session` đăng xuất. Phiên hết hạn sau 8 giờ.

Không đặt token/callback/account trong query URL. Không CORS wildcard.
OpenAPI/Swagger bị tắt để không lộ API ngoài phiên quản trị.

## Đầu vào và chạy batch

Mỗi dòng đúng ba thành phần không rỗng:

```text
user@example.com|your-password|YOUR_BASE32_TOTP_SECRET
```

Chỉ dùng secret TOTP thật của tài khoản được phép sử dụng, không dùng mã 6 chữ số.
Dòng trắng bỏ qua; dòng sai bị lọc kèm số dòng/lý do. Password giữ nguyên khoảng trắng.
Tối đa 5 MiB UTF-8 và 10.000 dòng. Không sửa/xóa file account gốc.

Proxy có thể là URL (`http://user:pass@host:8080`, `socks5://host:1080`), `host:port`,
`host:port:user:pass`, hoặc `http|host|port|user|pass`. Proxy sai không tự fallback
sang direct. Proxy trống nghĩa là direct. Không ghi mật khẩu proxy vào log.

`POST /api/accounts/validate`: `{"accounts_text":"..."}` trả preview đã che,
số dòng hợp lệ/bị lọc và cảnh báo trùng email; không đăng nhập tài khoản.

`POST /api/jobs`:

```json
{
  "accounts_text": "YOUR_ACCOUNT_ROWS",
  "proxies_text": "",
  "mode": "sequential",
  "workers": 1,
  "timeout": 180
}
```

`parallel` cho phép 1–16 worker. Một email và một proxy chỉ có một attempt đồng thời;
proxy giữ nguyên xuyên suốt attempt. Nếu có ít proxy, mức song song giảm tương ứng.
Chạy một lượt danh sách rồi kết thúc, không xoay lại vô hạn. Không tự retry sai
password/TOTP, phone verify, 403 hoặc rate limit; `invalid_state` được khởi tạo phiên
mới tối đa một lần. Token-exchange thất bại không rõ kết quả không bị gửi lặp lại.

Các API quản lý:

- `GET /api/health`: API và dependency auth có sẵn hay chưa.
- `GET /api/jobs`: lịch sử batch, dùng để tìm lại sau restart.
- `GET /api/jobs/{id}`: tiến độ, từng account và lỗi an toàn; không có token.
- `POST /api/jobs/{id}/stop`: ngừng lấy account mới; request đang chờ có thể cần
  đến timeout hiện tại (tối đa 30 giây). Kết quả đã lưu không bị mất.
- `GET /api/jobs/{id}/exports/{kind}`: tải `success`, `errors`, `phone_verify`,
  `filtered` hoặc `cancelled` dạng JSON array.

## Link và callback thủ công

`POST /api/oauth/links` với `{"email":"user@example.com","proxy":""}` trả link
desktop-auth và `session_id`. Mỗi phiên có PKCE/state riêng, hết hạn sau 10 phút.
API không trả `code_verifier` và không tự đăng nhập khi chỉ tạo link.

Mở link trong trình duyệt và hoàn tất đăng nhập. Tool **không mở listener 1455**;
trình duyệt có thể báo không kết nối được localhost. Sao chép nguyên URL callback
trên thanh địa chỉ, rồi `POST /api/oauth/complete`:

```json
{"session_id":"RETURNED_SESSION_ID","callback_url":"YOUR_LOCALHOST_CALLBACK_URL"}
```

Callback chỉ được tiêu thụ một lần, phải đúng host/port/path/state và chưa hết hạn.
Thành công tải về mảng JSON. Proxy của trình duyệt thủ công do bạn cấu hình;
tool chỉ kiểm soát proxy của HTTP token exchange.

## Kết quả và bảo mật

`success.json` chỉ gồm `id`, `email`, `account.id`, `tokens.id_token`,
`tokens.access_token`, `tokens.refresh_token`. ID bản ghi là UUID riêng;
account ID và email lấy từ ID token đã xác thực chữ ký/issuer/audience/thời hạn.
Token ChatGPT session không được dùng giả làm token Codex.

Phone verification có nhãn chính xác `Phone number verify`. Trang/redirect CAPTCHA-
Turnstile hoặc workspace không xác định được sẽ báo yêu cầu thao tác riêng. Metadata
Sentinel `dx` được xử lý giống code CheckLive gốc; chỉ metadata không bị coi là CAPTCHA.
Không tự giải xác minh điện thoại và không thêm solver ngoài repo.

`data/runs/<id>/run.json` là snapshot bền vững và **chứa token của kết quả thành công**.
File có quyền 0600 và thư mục 0700 trên POSIX. Trên Windows cần hạn chế ACL thư mục
cho chính người dùng chạy tool. Không đưa `data/`, `.env`, account hoặc token vào Git.
Nếu đĩa vẫn đầy khi khởi động lại, cấu hình đã có được đọc mà không ghi lại; API
chạy chế độ degraded để tải kết quả đã xác nhận, từ chối batch mới cho đến khi
giải phóng dung lượng và khởi động lại.
Nếu lỗi ghi xảy ra giữa batch, account đang xử lý chuyển thành lỗi và account chưa
chạy chuyển thành cancelled. Khi đĩa không ghi được cả trạng thái lỗi, các báo cáo
đó là bản phục hồi trong RAM (`batch_error.recovery_view`); tải chúng trước khi thoát.
Các success đã được xác nhận trên đĩa vẫn được giữ nguyên.
Password/secret đầu vào chỉ giữ trong bộ nhớ; restart không tự tiếp tục tài khoản
chưa xử lý, chúng chuyển thành cancelled với lý do interrupted. Kết quả đã lưu vẫn
tải được. Lỗi đĩa dừng batch và không báo success giả.

Không công khai dịch vụ ra LAN/Internet, không tắt TLS verification, không dùng tool
để vượt khóa tài khoản/rate limit. Lưu token xuất ra như lưu mật khẩu.

## Test offline

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m compileall -q autobuild_json
.venv/bin/python -m build
```

Kiểm thử giao diện bằng Node (chỉ phục vụ phát triển, không cần để chạy tool):

```bash
npm ci
npm run test:ui
npm run test:browser
```

Browser test sử dụng Chrome sẵn trên máy; đặt `CHROME_PATH` nếu khác
`/usr/bin/google-chrome`. Xem [tests/ui/acceptance.md](tests/ui/acceptance.md).

Test auth dùng methods thực từ checkout cố định với HTTP giả; không dùng token
trong `json.txt` hay gửi request đăng nhập account thật. Hai cảnh báo deprecation
của bộ TestClient hiện xuất hiện với các dependency mới nhất, không phải test lỗi.
HTTP 429 hiện báo RATE_LIMITED nhưng chưa hiển thị thời gian Retry-After của provider.

## Bản nháp giao diện

[Xem bản nháp Superdesign](https://p.superdesign.dev/draft/9de80bba-e738-47a7-bb54-cbb124bfc7d9).
Bản nháp dùng dữ liệu giả và tài nguyên trình bày trên canvas. Giao diện local đã
được chuyển sang assets nội bộ và dữ liệu thật từ API, không dùng các con số minh họa.
