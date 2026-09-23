# AutoBuildJsonCockplit-Tools — đặc tả thiết kế HTTP

Ngày: 2026-09-23  
Trạng thái: Đã duyệt và triển khai; UI Superdesign v2 đã được duyệt/ghép. Xem `docs/implementation-status.md` cho kiểm chứng và giới hạn.

## 1. Mục tiêu và lựa chọn đã thống nhất

Xây dựng tool FastAPI kèm web UI để xử lý các tài khoản do người vận hành sở hữu hoặc được phép sử dụng, thực hiện OAuth Codex và xuất JSON tương thích `Auto-Oauth-Codex-9Router/json.txt`.

Yêu cầu của người dùng:

- Tên project giữ nguyên `AutoBuildJsonCockplit-Tools`.
- Đầu vào là `Email|Password|2FA Secret`, mỗi dòng một tài khoản; lọc dòng thiếu thành phần.
- Tạo link có cấu trúc `https://chatgpt.com/codex/desktop-auth?authorize_url=...&codex_streamlined_login=true&no_universal_links=1`.
- Có chế độ tuần tự một tài khoản mỗi lần hoặc đa luồng, hỗ trợ proxy.
- Xuất JSON khi OAuth thành công, lỗi kèm lý do khi thất bại, và nhóm riêng `Phone number verify`.
- Dùng HTTP và phần auth từ `https://github.com/tmq9999/Check-Account-ChatGPT.git`; không dùng Playwright.

Các mặc định thiết kế để duyệt: tool chạy local, không cần 9router; web UI do FastAPI phục vụ; một process ứng dụng và một batch đang chạy tại một thời điểm; mặc định một worker, cho phép cấu hình 1–16 worker. Không triển khai public server, không cần Redis/Celery hay frontend framework ở phiên bản đầu.

## 2. Căn cứ khảo sát

- Project OAuth mẫu: commit `7895f5d` trong workspace. Luồng hiện tại lấy authorize URL từ 9router, đăng nhập qua `checklive.auth`, chuyển callback cho 9router rồi poll kết quả. Project chạy tuần tự và chưa có FastAPI/UI.
- Repo auth: commit `791beb350370c191bbe8ddbe0b4a3fa0072620d9`, nhánh `main`, được đọc trực tiếp từ GitHub. Không chạy mã hoặc đăng nhập tài khoản trong giai đoạn khảo sát.
- `AuthSession(proxy=...)` tạo `curl_cffi` session, cookie jar và device ID riêng; hỗ trợ từng bước email, password, TOTP. `generate_totp()` đã có sẵn.
- `AuthSession.login()` trả access token của phiên ChatGPT, không trả đủ `id_token`, `access_token`, `refresh_token` của OAuth Codex. Không được dùng token này giả làm kết quả Codex.
- Parser gốc có thể dừng toàn batch khi gặp dòng lỗi; worker/output gốc có hành vi lưu dữ liệu nhạy cảm và thay đổi file đầu vào. Tool mới không tái sử dụng các hành vi này.
- Phần debug gốc có thể ghi cookie/response; tích hợp phải vô hiệu hóa debug thô và dùng lỗi được chuẩn hóa.
- Repo auth có hỗ trợ tùy chọn cho một module Sentinel ngoài repo. Không coi module đó là có sẵn, không tự tải/chạy solver bổ sung, không hứa đăng nhập thành công qua mọi challenge.
- JSON mẫu là một mảng. Trong bản ghi đã đối chiếu, `account.id` tương ứng claim `["https://api.openai.com/auth"]["chatgpt_account_id"]`; `id` của bản ghi là UUID riêng, không phải account ID hay token subject. Giá trị token thật không được đưa vào tài liệu/log/fixture.

Tài liệu chính thức đã tham khảo: <https://developers.openai.com/codex/auth/> mô tả đăng nhập trình duyệt và callback localhost. Tài liệu này không xác nhận tính ổn định của các endpoint đăng nhập HTTP nội bộ hoặc toàn bộ query desktop-auth mà người dùng cung cấp; những phần đó phải được kiểm chứng bằng test và smoke test có kiểm soát.

## 3. Kiến trúc được chọn

Một ứng dụng FastAPI phục vụ API và UI cùng origin. Các thao tác HTTP đồng bộ của auth chạy trong worker pool có giới hạn, không chặn event loop của FastAPI.

Các module có trách nhiệm độc lập:

1. `input_parser`: đọc văn bản tài khoản và proxy, trả dòng hợp lệ cùng danh sách bị loại.
2. `oauth`: tạo PKCE/state/link, kiểm tra callback, đổi code và xác thực token response.
3. `checklive_adapter`: tích hợp các bước auth từ repo được chỉ định, quản lý session/proxy, phát hiện challenge và chuẩn hóa lỗi.
4. `runner`: hàng đợi, worker pool, hủy tác vụ, deadline và cập nhật trạng thái.
5. `results`: ghi kết quả bền vững theo batch và tạo file export đúng schema.
6. `api` và `ui`: khởi chạy, theo dõi, dừng và tải kết quả; không tự chứa logic đăng nhập.

So với việc sao chép toàn bộ CLI checker, adapter nhỏ giúp không kế thừa chức năng xóa input, kiểm tra subscription hoặc log nhạy cảm. So với viết lại toàn bộ đăng nhập, cách này sử dụng đúng auth mà người dùng chỉ định và giới hạn phần phải duy trì.

Repo auth là dependency source riêng, cố định commit đã khảo sát. Khi triển khai, hướng dẫn/setup chuẩn bị checkout và đặt `CHECKLIVE_PATH`; không pip-install một repo chưa có package manifest, không import entrypoint `main.py`, không tự pull nhánh mới khi chạy. Có kiểm tra tương thích module/method trước khi nhận batch. Không sửa project mẫu hoặc checkout auth của người dùng.

## 4. Quy tắc đầu vào

- Nhận upload UTF-8/UTF-8 BOM hoặc paste text; giới hạn 5 MiB và 10.000 dòng cho mỗi batch.
- Bỏ qua dòng trắng. Các dòng khác phải có đúng ba trường phân cách bằng `|` và cả ba phải không rỗng.
- Trim email, chuẩn hóa TOTP secret theo quy tắc của auth; giữ nguyên ký tự password, không âm thầm trim password.
- Loại dòng thiếu/thừa trường, email sai hình thức rõ ràng hoặc secret không thể dùng làm TOTP; lưu số dòng và lý do, không lưu nguyên dòng chứa bí mật vào báo cáo.
- Không dùng dấu `:` làm dấu phân cách account; không yêu cầu tiền tố `LIVE`.
- Mỗi dòng hợp lệ có ID công việc riêng. Dòng trùng email được cảnh báo; không tự xóa hoặc gộp kết quả theo email. Không chạy hai dòng cùng email đồng thời, kể cả khi bật đa luồng.
- Không xóa/sửa file account do người dùng đưa vào.

Proxy nhận URL `http://`, `https://`, `socks5://`, `socks5h://`, dạng `host:port`, `host:port:user:pass`, và dạng có protocol phân cách `:` hoặc `|` của repo mẫu. Validate host/port và encode user/password đúng chuẩn; không đưa thông tin xác thực proxy vào lỗi hay log. Proxy sai cấu hình phải được báo trước khi chạy, không âm thầm chuyển sang kết nối trực tiếp.

## 5. Link OAuth, callback và token

Mỗi lần thử tạo `code_verifier` ngẫu nhiên theo PKCE, `code_challenge = BASE64URL(SHA256(verifier))` không padding, và `state` ngẫu nhiên riêng. Verifier chỉ nằm ở backend; state có thời hạn 10 phút và dùng một lần.

Authorize URL dùng các giá trị nền từ mẫu người dùng:

- Endpoint: `https://auth.openai.com/oauth/authorize`.
- `response_type=code`, `client_id=app_EMoamEEZ73f0CkXaXp7hrann`.
- `redirect_uri=http://localhost:1455/auth/callback`.
- Scope: `openid profile email offline_access api.connectors.read api.connectors.invoke`.
- `code_challenge_method=S256`, `id_token_add_organizations=true`.
- `codex_cli_simplified_flow=true`, `codex_streamlined_login=true`.
- `originator=Codex Desktop`, `codex_app_version=26.820.60940` là mặc định có thể cấu hình, không khẳng định đây là version mới nhất.
- `source_surface_stable_id` và `codex_origin_stable_id` dùng cùng UUID ổn định của lần cài đặt, không sao chép UUID trong link mẫu.

Encode query authorize một lần, sau đó encode toàn bộ authorize URL làm giá trị `authorize_url` của desktop-auth. Không tái sử dụng state/challenge mẫu, không nối query thủ công. Link desktop-auth được hiển thị/copy ở UI; worker HTTP sử dụng authorize URL bên trong, không phụ thuộc HTML/JavaScript của trang desktop-auth.

Luồng HTTP tự động:

1. Tạo session auth và gán proxy cho lần thử hiện tại.
2. Thực hiện email/password/TOTP qua adapter, kiểm tra phone/email OTP/challenge sau từng response và redirect. Chỉ gọi các bước cần để lập phiên đăng nhập.
3. Dùng cùng session để đi theo authorize URL Codex và xử lý chọn account/workspace/consent được hỗ trợ.
4. Chỉ chuyển tiếp đến HTTPS origin được cho phép và các route phù hợp; kiểm tra từng redirect, kể cả trong phần đăng nhập, thay vì tự động đi theo URL tùy ý. Giới hạn số redirect và kích thước response.
5. Khi nhận URL callback chính xác `http://localhost:1455/auth/callback`, chặn trước khi gửi request. Không mở listener cổng 1455 và không chuyển callback qua proxy.
6. Kiểm tra callback chứa đúng một code và một state, state khớp phiên chưa hết hạn/chưa dùng; xử lý `error` OAuth riêng. Không nhận host/path/port gần giống hoặc URL có userinfo.
7. Đổi code một lần tại `https://auth.openai.com/oauth/token` với `grant_type=authorization_code`, client ID, verifier và redirect URI của phiên. Dùng cùng proxy đã gán; TLS verification bật.
8. Chỉ đánh dấu thành công khi token response hợp lệ, đủ ba token và có account ID/email phù hợp. Xác thực chữ ký ID token qua JWKS cùng issuer/audience/thời hạn; JWKS lấy từ issuer HTTPS cố định, không lấy URL từ token không tin cậy. Khi endpoint/metadata không tương thích, trả lỗi rõ ràng thay vì bỏ qua xác thực. Đối chiếu email claim với email input để tránh xuất nhầm tài khoản; không tự suy diễn alias email.

Top-level `id` của bản ghi export là UUID được tạo cho kết quả; `account.id` là claim account ID đã xác thực. Nếu có nhiều workspace không xác định được lựa chọn phù hợp, báo `WORKSPACE_SELECTION_REQUIRED`; không chọn workspace tùy ý.

Trong phiên bản đầu, link copy phục vụ xem/đối chiếu hoặc xử lý với API trả callback thủ công; không có browser callback tự động. API tạo link nhận email dự kiến, tùy chọn proxy cho token exchange và trả session ID/link/thời hạn; không cần password/secret và không tự gửi request đăng nhập. Người dùng hoàn tất đăng nhập bằng trình duyệt rồi dán URL callback trên thanh địa chỉ, kể cả khi localhost báo không kết nối được. API hoàn tất nhận session ID/callback URL và áp dụng toàn bộ kiểm tra PKCE/state/token/email ở trên. Proxy của trình duyệt thủ công do người dùng quản lý, tool không tự cấu hình hay bảo đảm trùng proxy. Phiên thao tác tay và phiên batch tách biệt; giới hạn 100 phiên thủ công chưa hết hạn, dọn phiên hết hạn và xóa verifier sau khi tiêu thụ.

## 6. Lập lịch, proxy và giới hạn

- `sequential`: đúng một account đang thực hiện OAuth, xử lý một lượt danh sách rồi kết thúc; không lặp vô hạn.
- `parallel`: worker pool 1–16, mặc định 1; số worker không thay đổi giữa batch.
- Gán proxy round-robin khi nhận một account mới. Giữ nguyên proxy cho toàn bộ lần thử, bao gồm đăng nhập, authorize, token exchange; không đổi IP giữa phiên, không xoay proxy để vượt chặn/rate limit.
- Session/cookie/verifier/state không được dùng chung giữa account. Một proxy có tối đa một lần thử đang chạy; nếu ít proxy hơn worker, mức song song thực tế giảm theo số proxy. Không có proxy thì dùng kết nối trực tiếp.
- Không gọi đăng nhập hàng loạt hoặc kiểm thử proxy tự động trước khi người dùng nhấn Start.
- Timeout request tối đa 30 giây, deadline account mặc định 180 giây; deadline bao gồm thời gian chờ/retry. Chỉ retry có giới hạn với lỗi mạng phù hợp; tối đa một lần khởi động lại phiên khi `invalid_state`. Không chồng retry mặc định của auth và runner tạo nhiều lần thử ngoài ý muốn.
- Không tự retry sai password/TOTP, deactivated, phone verify, email OTP hoặc challenge bắt buộc. `429` kết thúc attempt với `RATE_LIMITED`, ghi thời gian chờ đã làm sạch nếu có; không tăng worker hoặc đổi proxy để thử vượt giới hạn.
- Không tự gửi lại token-exchange POST khi trạng thái tiêu thụ code không rõ ràng sau lỗi mạng; báo `TOKEN_EXCHANGE_UNCERTAIN` và chỉ bắt đầu phiên mới theo hành động rõ ràng của người dùng.
- Stop ngừng lấy việc mới và hủy hợp tác các account đang chạy ở điểm kiểm tra kế tiếp. Request đang chờ có thể cần đến timeout hiện tại; không hứa dừng tức thì. Kết quả thành công đã ghi vẫn giữ nguyên.
- Đóng session trong `finally`. Xóa tham chiếu password/secret/verifier khỏi bộ nhớ khi kết thúc; không tuyên bố Python bảo đảm zeroization bộ nhớ.

## 7. Trạng thái và export

Trạng thái account: `queued`, `running`, `success`, `error`, `phone_verify`, `cancelled`.

Lỗi có `account_job_id`, `line_number`, `email`, `stage`, `code`, `reason`, `attempt`, `timestamp`; reason từ danh mục thông báo an toàn, không xuất nguyên exception/response body.

Mã lỗi tối thiểu: `INVALID_CREDENTIALS`, `ACCOUNT_DEACTIVATED`, `MFA_ERROR`, `EMAIL_OTP_REQUIRED`, `PROXY_ERROR`, `NETWORK_ERROR`, `TIMEOUT`, `RATE_LIMITED`, `AUTH_BLOCKED`, `ACTION_REQUIRED`, `INVALID_STATE`, `OAUTH_DENIED`, `WORKSPACE_SELECTION_REQUIRED`, `TOKEN_EXCHANGE_ERROR`, `TOKEN_EXCHANGE_UNCERTAIN`, `INVALID_TOKEN_RESPONSE`, `ACCOUNT_MISMATCH`, `CONFIGURATION_ERROR`.

Phone detection dựa trên page type/route/error chỉ rõ yêu cầu xác minh điện thoại; không chỉ dò sự xuất hiện của từ `phone` trong body, field profile hoặc phương thức MFA có sẵn. Kết quả có `status=phone_verify` và `reason=Phone number verify`, không thử lấy/nhập SMS.

Các file tải xuống:

- `success.json`: JSON array đúng schema bên dưới, không thêm status/password/secret/proxy vào mỗi record.
- `errors.json`: các account lỗi kèm lý do đã làm sạch.
- `phone_verify.json`: các account cần xác minh số điện thoại.
- `filtered.json`: số dòng bị loại và lý do; không chứa dòng account gốc.
- `cancelled.json`: account chưa hoàn tất do Stop, không chứa bí mật đầu vào.

Ví dụ cấu trúc minh họa, không phải token sử dụng được:

```json
[
  {
    "id": "00000000-0000-4000-8000-000000000001",
    "email": "user@example.com",
    "account": { "id": "00000000-0000-4000-8000-000000000002" },
    "tokens": {
      "id_token": "example-id-token",
      "access_token": "example-access-token",
      "refresh_token": "example-refresh-token"
    }
  }
]
```

Mỗi batch có thư mục riêng trong `data/runs/`, đường dẫn do backend sinh. Ghi snapshot atomic dưới lock; chỉ đánh dấu success sau khi ghi bền vững thành công. Lỗi đĩa dừng batch, hiển thị lỗi lưu trữ, không báo export thành công giả. Thư mục/file chứa token có quyền hạn chế (0700/0600 trên POSIX), nằm trong gitignore. Input password/secret không ghi vào database hoặc kết quả; sau restart không tự resume credential trong bộ nhớ, các việc chưa kết thúc chuyển thành `cancelled` với lý do `Interrupted by application restart`. Kết quả đã lưu vẫn đọc/tải được. Luồng callback thủ công trả JSON cùng schema qua response tải xuống được bảo vệ; không đưa token vào thông báo UI.

## 8. API và UI

API dự kiến:

- `GET /api/health`: trạng thái ứng dụng và dependency sẵn sàng, không lộ đường dẫn/secret.
- `POST /api/accounts/validate`: kiểm tra input; trả tổng hợp và preview đã che, không chạy OAuth.
- `POST /api/oauth/links`: tạo phiên PKCE thủ công và link tương ứng, không trả verifier.
- `POST /api/oauth/complete`: nhận callback của phiên thủ công và trả kết quả sau khi kiểm tra đầy đủ.
- `POST /api/jobs`: validate lại đầu vào và tạo batch; từ chối nếu đã có batch đang chạy.
- `GET /api/jobs/{id}`: tiến độ, trạng thái từng account và lỗi an toàn; không trả token.
- `POST /api/jobs/{id}/stop`: yêu cầu dừng.
- `GET /api/jobs/{id}/exports/{kind}`: tải file kết quả; `kind` thuộc enum cố định, không phải đường dẫn tùy ý.

UI tiếng Việt, bố cục công cụ một trang:

- Vùng import/paste accounts; preview số hợp lệ/bị lọc và lý do.
- Vùng proxy và chế độ chạy, worker, timeout.
- Start/Stop và tóm tắt chờ/đang chạy/thành công/lỗi/phone verify.
- Bảng account với email, stage, status, reason; bộ lọc theo trạng thái.
- Khu vực tạo/copy link OAuth và hoàn tất callback thủ công.
- Nút tải từng nhóm kết quả; không hiển thị token trong bảng hoặc console trình duyệt.

UI poll API một giây một lần khi tab đang mở và batch đang chạy; dừng poll khi kết thúc hoặc rời trang. Dùng HTML/CSS/JavaScript do FastAPI phục vụ, không bắt người dùng cài Node để chạy tool. Thiết kế hình thức UI sẽ thực hiện ở giai đoạn triển khai sau các bước duyệt của Superpowers.

## 9. Bảo mật và giới hạn phạm vi

- Bind mặc định `127.0.0.1`; không tự mở firewall hoặc expose ra LAN/Internet. Một Uvicorn worker, không dùng `--reload` khi xử lý batch.
- Bảo vệ UI/API bằng session quản trị local: setup tạo bootstrap token, đổi qua POST thành cookie HttpOnly/SameSite; không đặt token trong query URL. Kiểm tra Host/Origin và CSRF cho hành động thay đổi trạng thái. Không CORS wildcard; tài liệu API và export chịu cùng kiểm soát truy cập.
- Không gửi tài khoản, JSON mẫu, token hoặc source chứa bí mật sang dịch vụ thiết kế UI, analytics hay bên thứ ba ngoài các endpoint auth được yêu cầu. Dữ liệu mẫu để thiết kế/test hoàn toàn giả lập.
- Không dùng raw exception làm response; không log request body của API, OAuth query, callback, cookie, password, TOTP secret/code, access/refresh token hoặc proxy credentials. Access log không ghi query nhạy cảm; các response liên quan credential dùng `Cache-Control: no-store`.
- HTTP client xác minh TLS, giới hạn redirect/origin; không nhận endpoint OAuth tùy ý qua UI/API.
- Không tạo account, không kiểm tra subscription, không mua/nhập số điện thoại, không thêm solver CAPTCHA/Turnstile, không vượt MFA hoặc khóa tài khoản. Khi cần thao tác ngoài email/password/TOTP được hỗ trợ, trả trạng thái thích hợp và dừng attempt.
- Không chạy OAuth thật bằng dữ liệu trong `json.txt`; file đó chỉ dùng đối chiếu schema. Chỉ smoke-test mạng thật khi người dùng chủ động chọn tài khoản được phép và yêu cầu chạy.

## 10. Kiểm thử và điều kiện nghiệm thu

Viết test trước phần triển khai tương ứng, dùng credential giả và HTTP transport giả:

- Parser: BOM, newline, dòng thiếu/rỗng/thừa trường, password có khoảng trắng, TOTP sai, input size/count, duplicate email, không sửa file gốc.
- Proxy: parse định dạng, credentials có ký tự đặc biệt, proxy sai không fallback direct, round-robin và sticky proxy suốt phiên.
- OAuth: encode nested URL, PKCE chuẩn, state/verifier độc lập, expiry/replay, redirect allowlist, callback path/host/port chính xác, duplicate query params, denied/invalid state và token exchange không lặp mù.
- Auth adapter: email/password/TOTP, email OTP, phone ở từng bước, false-positive phone field, 401/403/429, deactivated, deadline, exception redaction, cleanup session.
- Token/export: xác thực JWT với JWKS giả, issuer/audience/expiry, email/account mismatch, thiếu token/claim, UUID bản ghi độc lập, schema khớp JSON mẫu.
- Runner: concurrency thực tế không vượt giới hạn, không trộn session/state, cùng email không đồng thời, cùng proxy không đồng thời, Stop giữ kết quả đã ghi, accounting của mọi trạng thái và batch conflict.
- Storage: atomic write, lỗi đĩa, restart/interrupted state, quyền file, không lộ credential đầu vào, export rỗng là `[]` hợp lệ.
- API/UI: auth/CSRF/Origin, không lộ token qua polling, validate không thực hiện mạng ngoài, start/progress/stop/download hoạt động end-to-end với backend OAuth giả, UI báo lỗi lưu trữ hoặc dependency thiếu.

Không coi test giả lập pass là bằng chứng OAuth live thành công. Bàn giao phải nêu rõ phần đã kiểm chứng offline và phần chưa smoke-test thực tế. Chỉ ghi nhận OAuth thành công khi token exchange và các bước xác thực/ghi kết quả của tài khoản đó thực sự hoàn tất.

## 11. Bước tiếp theo theo Superpowers

Người dùng duyệt hoặc sửa đặc tả này. Sau khi duyệt, tạo implementation plan bằng skill `writing-plans`, rồi trình kế hoạch và phương thức thực thi để người dùng chọn trước khi viết product code/cài dependencies. Tài liệu này không thay cho kế hoạch đã duyệt.

Project đã có Git riêng trên nhánh `feat/http-oauth`; không sửa hoặc commit vào repo tham chiếu của người dùng.
