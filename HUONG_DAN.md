# Hướng dẫn cài đặt – Tự động đăng lại bài fanpage

Chạy hoàn toàn trên GitHub (miễn phí), không cần máy tính bật. Làm 1 lần, khoảng 30–40 phút.

**Mỗi sáng 8:57**: lấy bài mới của `cuahangdientugiatot.dn` qua Apify → AI bỏ bài sự kiện, bỏ ảnh có địa chỉ shop khác, viết lại đúng quy tắc → gửi nháp sang **Telegram** kèm nút **✅ Đăng / ❌ Bỏ**.
**Từ 9h–23h, mỗi 10 phút**: đọc nút bạn bấm → đăng lên fanpage shop và báo lại link.
Muốn sửa chữ: trên Telegram **trả lời (reply) tin nháp** bằng nội dung mới → bot gửi lại bản đã sửa để bạn bấm Đăng.

---

## Bước 1 – Tạo bot Telegram (2 phút)
1. Trên Telegram, tìm **@BotFather** → gửi `/newbot` → đặt tên (vd: `DMX 251 Dang Bai`) và username kết thúc bằng `bot`.
2. BotFather trả về **token** dạng `123456:ABC...` → lưu lại = `TELEGRAM_BOT_TOKEN`.
3. Mở bot vừa tạo, bấm **Start**, gửi 1 tin bất kỳ (vd "hi").
4. Mở trình duyệt: `https://api.telegram.org/bot<TOKEN>/getUpdates` (thay `<TOKEN>`). Tìm `"chat":{"id":123456789` → số đó = `TELEGRAM_CHAT_ID`.

## Bước 2 – Token Apify (1 phút)
console.apify.com → **Settings → API & Integrations** → copy **Personal API token** = `APIFY_TOKEN`.

## Bước 3 – Mã khóa AI Gemini, miễn phí (2 phút)
Vào **aistudio.google.com** → **Get API key** → **Create API key** → copy = `GEMINI_API_KEY`.
(Muốn dùng Claude thay Gemini: tạo key ở console.anthropic.com, đặt secret `ANTHROPIC_API_KEY` và biến `AI_PROVIDER` = `claude`.)

## Bước 4 – Token đăng bài fanpage, loại không hết hạn (10–15 phút)
1. Vào **developers.facebook.com** → đăng nhập tài khoản quản trị fanpage → **My Apps → Create App**.
   Chọn mục đích kiểu *"Other" / "Quản lý mọi thứ trên Trang"*, loại **Business**, đặt tên bất kỳ. Không cần gửi xét duyệt.
2. Mở **Tools → Graph API Explorer**:
   - Ô *Meta App*: chọn app vừa tạo.
   - *User or Page*: **Get User Access Token**, tick quyền: `pages_show_list`, `pages_read_engagement`, `pages_manage_posts` → **Generate Access Token** → chọn đúng trang *Điện Máy Liên Chiểu Đà Nẵng - 251 Nguyễn Lương Bằng* → đồng ý.
3. Bấm biểu tượng **ⓘ** cạnh token → **Open in Access Token Tool** → bấm **Extend Access Token** → copy token dài (60 ngày).
4. Quay lại Graph API Explorer, dán token dài vào ô Access Token, gõ truy vấn `me/accounts` → **Submit**.
   Trong kết quả, ở mục trang shop, copy giá trị `access_token` → đây là **token trang không hết hạn** = `FB_PAGE_TOKEN`.
   (Kiểm tra: dán vào Access Token Debugger thấy *Expires: Never* là đúng.)

## Bước 5 – Tạo kho GitHub và dán mã (5 phút)
1. Tạo tài khoản **github.com** (nếu chưa có) → **New repository** → tên `repost-fanpage` → chọn **Private** → Create.
2. Bấm **uploading an existing file** → kéo thả **toàn bộ nội dung** thư mục `repost-fanpage` (gồm `main.py`, `HUONG_DAN.md` và thư mục `.github`) → **Commit changes**.
   Lưu ý: thư mục `.github` là thư mục ẩn – trên Windows bật *View → Hidden items* để thấy và kéo vào.
   Nếu sau khi tải lên không thấy thư mục `.github` trong kho: bấm **Add file → Create new file**, gõ tên
   `.github/workflows/fetch.yml`, dán nội dung file `fetch.yml` → Commit. Làm tương tự với `approve.yml`.
3. Vào **Settings → Secrets and variables → Actions → New repository secret**, tạo lần lượt 5 secret:
   `APIFY_TOKEN`, `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `FB_PAGE_TOKEN`.

## Bước 6 – Chạy thử
1. Tab **Actions** → nếu GitHub hỏi, bấm bật workflows.
2. Chọn **Lay bai moi va gui nhap** → **Run workflow**. Sau 1–3 phút, Telegram nhận nháp.
3. Bấm **✅ Đăng** trên 1 bài → chọn **Doc nut duyet va dang bai** → **Run workflow** (để không phải chờ 10 phút) → Telegram báo link bài đã đăng.

Có lỗi: bấm vào lần chạy màu đỏ trong tab Actions → chụp màn hình gửi Claude.

---

## Chi phí
- GitHub Actions, Telegram, Facebook API: 0đ.
- Gemini: gói miễn phí đủ dùng.
- Apify Facebook Posts Scraper: khoảng 5 USD / 1.000 bài; mỗi ngày lấy tối đa 15 bài gần nhất (đổi bằng biến `RESULTS_LIMIT`). Gói Free có 5 USD tín dụng/tháng.

## Tùy chỉnh (Settings → Secrets and variables → Actions → tab Variables)
- `AI_PROVIDER`: `gemini` (mặc định) hoặc `claude`.
- `GEMINI_MODEL`: đổi model nếu Google ngừng model mặc định.
- Đổi giờ chạy: sửa dòng `cron` trong `.github/workflows/fetch.yml` (giờ UTC = giờ VN − 7).
- Quy tắc viết bài, địa chỉ: sửa phần `ADDRESS_LINES` và `RULES` ở đầu `main.py`.

## Sau này muốn tự đăng không cần duyệt
Nhắn Claude để chỉnh: bỏ bước gửi nút, đăng thẳng sau khi viết lại.
