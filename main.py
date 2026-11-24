"""
Tự động lấy bài fanpage nguồn (Apify) -> AI viết lại -> duyệt qua Telegram -> đăng lên fanpage shop.

  python main.py fetch     # chạy mỗi sáng: lấy bài mới, viết lại, gửi nháp sang Telegram
  python main.py approve   # chạy mỗi 10 phút: đọc nút bấm Telegram, đăng / bỏ bài
  python main.py test-ai   # chạy thử phần viết lại với 1 bài mẫu (không gửi gì)

Biến môi trường (đặt trong GitHub Secrets):
  APIFY_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, FB_PAGE_TOKEN
  GEMINI_API_KEY  (hoặc ANTHROPIC_API_KEY nếu dùng Claude)
Tuỳ chọn: SOURCE_URL, AI_PROVIDER (gemini|claude), GEMINI_MODEL, CLAUDE_MODEL, LOOKBACK_HOURS
"""
import base64
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DRAFT_DIR = ROOT / "drafts"

SOURCE_URLS = [u.strip() for u in (os.getenv("SOURCE_URL") or "https://www.facebook.com/cuahangdientugiatot.dn,https://www.facebook.com/profile.php?id=61556486280886").split(",") if u.strip()]
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "26"))
MAX_PER_DAY = int(os.getenv("MAX_PER_DAY", "4"))
AI_PROVIDER = (os.getenv("AI_PROVIDER") or "gemini").lower()
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-flash-latest"
GEMINI_MODELS = [GEMINI_MODEL, "gemini-flash-lite-latest"]
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL") or "claude-haiku-4-5"
GRAPH = "https://graph.facebook.com/v27.0"
VN_TZ = timezone(timedelta(hours=7))

ADDRESS_LINES = [
    "📍 Điện Máy Xanh – 251 Nguyễn Lương Bằng (Ngã 3 Nguyễn Chánh), P. Liên Chiểu, Đà Nẵng",
    "🗺️ Dẫn đường: https://maps.app.goo.gl/44cs4Fyzo4LKq88q6",
]
MAX_LINES = 7
MAX_CHARS = 400

RULES = f"""Bạn là nhân viên viết bài bán hàng cho fanpage cửa hàng Điện Máy Xanh 251 Nguyễn Lương Bằng, Liên Chiểu, Đà Nẵng.
Nhiệm vụ: đọc bài gốc (chữ + ảnh) từ fanpage của một cửa hàng khác và trả về JSON.

1) is_sale_post: true nếu bài giới thiệu sản phẩm / giá / khuyến mãi để bán hàng. false nếu là bài cảm ơn khách, sự kiện,
   bốc thăm, tuyển dụng, livestream, chúc mừng, tin tức... (những bài này bỏ qua).
2) post: bài viết lại (chỉ phần nội dung, KHÔNG ghi địa chỉ, KHÔNG ghi link bản đồ — hệ thống tự thêm 2 dòng địa chỉ):
   - Không sao chép nguyên văn; viết lại bằng từ ngữ của bạn.
   - Dùng từ ngữ không bị Facebook hạn chế: không dùng "rẻ nhất", "cam kết", "100%", "inbox", "vay tiền", "giảm cân",
     từ giật gân, viết hoa toàn bộ quá nhiều, không kêu gọi bình luận/chia sẻ.
   - Giữ CHÍNH XÁC tên sản phẩm (cả mã model), giá bán, giá gốc, khuyến mãi, thời hạn ưu đãi nếu có.
   - KHÔNG bịa thêm bất kỳ thông tin nào không có trong bài gốc/ảnh (không thêm quà tặng, bảo hành, trả chậm... nếu gốc không có).
   - "trả góp 0%" luôn viết thành "trả chậm 0%".
   - Phong cách bán hàng chuyên nghiệp, gọn gàng. Tối đa {MAX_LINES - 2} dòng và tối đa 250 ký tự.
   - Emoji CHỈ được dùng ở dòng tiêu đề (dòng đầu) và dòng giá tiền. Các dòng khác không có emoji.
   - Không dùng hashtag.

Mẫu bài đã được chủ shop duyệt:
🎥 DJI OSMO NANO 128GB – NHỎ GỌN, CÔNG NGHỆ ĐỈNH
Camera kỹ thuật số nhỏ gọn, dễ mang theo, ghi lại trọn vẹn mọi khoảnh khắc đáng nhớ.
💰 Giá chỉ 8.460.000đ
Ghé shop xem máy trực tiếp và nhận tư vấn chi tiết.

Chỉ trả về JSON đúng dạng: {{"is_sale_post": true, "reason": "...", "post": "..."}}"""


# ---------------------------------------------------------------- tiện ích
def env(name):
    v = os.getenv(name)
    if not v:
        sys.exit(f"Thiếu biến môi trường {name} (kiểm tra GitHub Secrets)")
    return v.strip()


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen": [], "tg_offset": 0}


def save_state(st):
    st["seen"] = st["seen"][-500:]
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def draft_path(pid):
    return DRAFT_DIR / f"{pid}.json"


def load_draft(pid):
    p = draft_path(pid)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_draft(d):
    DRAFT_DIR.mkdir(exist_ok=True)
    draft_path(d["id"]).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def delete_draft(d):
    for img in d.get("images", []):
        (ROOT / img).unlink(missing_ok=True)
    draft_path(d["id"]).unlink(missing_ok=True)


# ---------------------------------------------------------------- chuẩn hoá bài viết
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍⃣]+"
)


def normalize_post(body: str):
    """Áp các quy tắc cứng bằng code. Trả về (bài hoàn chỉnh, danh sách cảnh báo)."""
    warnings = []
    body = body.replace("\r", "").strip()
    body = re.sub(r"trả\s*góp", lambda m: "Trả chậm" if m.group(0)[0].isupper() else "trả chậm", body, flags=re.I)
    lines = [l.strip() for l in body.split("\n") if l.strip()]
    # bỏ mọi dòng địa chỉ / bản đồ / hashtag AI lỡ thêm vào
    lines = [l for l in lines if not re.search(r"maps\.app|goo\.gl|Nguyễn Lương Bằng|Dẫn đường|^#", l)]
    lines = [re.sub(r"\s#\S+", "", l) for l in lines]
    # emoji chỉ ở dòng đầu và dòng có giá tiền
    clean = []
    for i, l in enumerate(lines):
        has_price = bool(re.search(r"\d[\d.,]*\s*(đ|₫|vnđ|vnd|triệu|tr\b|k\b)", l, re.I))
        if i != 0 and not has_price:
            l2 = EMOJI_RE.sub("", l).strip()
            if l2 != l:
                l = re.sub(r"\s{2,}", " ", l2)
        if l:
            clean.append(l)
    lines = clean
    full = lines + ADDRESS_LINES
    text = "\n".join(full)
    if len(full) > MAX_LINES:
        warnings.append(f"{len(full)} dòng (tối đa {MAX_LINES})")
    if len(text) > MAX_CHARS:
        warnings.append(f"{len(text)} ký tự (tối đa {MAX_CHARS})")
    return text, warnings


# ---------------------------------------------------------------- AI
def _parse_json(s):
    s = s.strip()
    s = re.sub(r"^```(json)?|```$", "", s, flags=re.M).strip()
    m = re.search(r"\{.*\}", s, re.S)
    return json.loads(m.group(0) if m else s)


def ai_call(prompt, images, temperature=0.7):
    """Gọi AI (Gemini hoặc Claude) với ảnh + chữ, trả về dict JSON."""
    for attempt in range(6):
        try:
            if AI_PROVIDER == "claude":
                content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                        "data": base64.b64encode(b).decode()}} for b in images]
                content.append({"type": "text", "text": prompt})
                r = requests.post("https://api.anthropic.com/v1/messages", timeout=120, headers={
                    "x-api-key": env("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01",
                    "content-type": "application/json"},
                    json={"model": CLAUDE_MODEL, "max_tokens": 2000, "temperature": temperature,
                          "messages": [{"role": "user", "content": content}]})
                r.raise_for_status()
                return _parse_json(r.json()["content"][0]["text"])
            parts = [{"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode()}} for b in images]
            parts.append({"text": prompt})
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODELS[attempt % len(GEMINI_MODELS)]}:generateContent",
                params={"key": env("GEMINI_API_KEY")}, timeout=120,
                json={"contents": [{"parts": parts}],
                      "generationConfig": {"responseMimeType": "application/json", "temperature": temperature}})
            r.raise_for_status()
            return _parse_json(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        except Exception as e:  # mạng chập chờn / quá hạn mức
            print("AI lỗi, thử lại:", e)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("AI không trả lời được sau 6 lần")


def ai_rewrite(text, images, extra=""):
    """images: list[bytes] (jpeg). Trả về dict theo RULES (chỉ viết bài, KHÔNG chấm ảnh)."""
    prompt = RULES + extra + "\n\n--- BÀI GỐC ---\n" + (text or "(không có chữ)") + f"\n--- Số ảnh đính kèm: {len(images)} ---"
    return ai_call(prompt, images)


# ---------------------------------------------------------------- kiểm tra ảnh (từng ảnh, tách riêng)
IMAGE_CHECK_PROMPT = """Bạn là máy đọc chữ trong ảnh quảng cáo điện máy. CHỈ dựa vào những gì THẬT SỰ nhìn thấy trong ảnh này.
1) ocr: chép lại nguyên văn TOÀN BỘ chữ nhìn thấy trong ảnh (giữ dấu tiếng Việt, mỗi cụm 1 dòng).
2) store_info: liệt kê nguyên văn (chép đúng từng chữ như trong ocr) các cụm chữ là:
   - số điện thoại / hotline của một cửa hàng cụ thể,
   - địa chỉ: số nhà, tên đường, kiệt/hẻm, phường/xã, quận/huyện,
   - tên riêng của một cửa hàng / chi nhánh / siêu thị cụ thể.
   KHÔNG liệt kê: logo hoặc chữ thương hiệu chuỗi (Điện Máy Xanh, Thế Giới Di Động, TopZone, Bách Hóa Xanh),
   tên hãng/sản phẩm/model (Livotec, Karofi, Samsung...), giá tiền, % giảm giá, thông số, chữ khuyến mãi chung.
   Không thấy thì để mảng rỗng. Tuyệt đối không đoán.
Chỉ trả về JSON: {"ocr": "...", "store_info": ["..."]}"""

CHAIN_WHITELIST = ["dien may xanh", "dienmayxanh", "the gioi di dong", "thegioididong", "topzone",
                   "bach hoa xanh", "an khang", "dmx", "tgdd"]
PHONE_RE = re.compile(r"(?<![\d.,])(?:\+?84|0)[\s.\-]?[235789](?:[\s.\-]?\d){8}(?![\d.,]*\s*(?:đ|₫|vnd|vnđ))", re.I)
ADDR_RE = re.compile(r"\b(đường|đ\.|phường|p\.|quận|q\.|huyện|xã|thị xã|thị trấn|tp\.?|thành phố|kiệt|hẻm|ngõ|ngã [34]|"
                     r"chi nhánh|cn\b|số nhà|tổ \d|thôn)|\b\d{1,4}[a-z]?\s*/\s*\d|\b\d{1,4}[a-z]?\s+[A-ZĐ][a-zà-ỹ]+\s+[A-ZĐ]", re.I)
STORE_RE = re.compile(r"\b(cửa hàng|siêu thị|shop|điện máy|chi nhánh)\s+\S+", re.I)


def _norm(s):
    import unicodedata
    s = unicodedata.normalize("NFD", (s or "").lower()).replace("đ", "d")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def verify_store_info(ocr, frags):
    """Code tự kiểm lại: chỉ giữ cụm có THẬT trong chữ đọc được của ảnh và đúng dạng địa chỉ/SĐT/tên shop."""
    n_ocr = _norm(ocr)
    found = []
    for f in frags or []:
        f = str(f).strip()
        nf = _norm(f)
        if len(nf) < 4 or nf not in n_ocr:
            continue  # AI nói có nhưng chữ đó không nằm trong ảnh -> bỏ qua
        rest = nf
        for w in CHAIN_WHITELIST:
            rest = rest.replace(w, " ")
        if not re.sub(r"\s+", "", rest):
            continue  # chỉ là tên chuỗi Điện Máy Xanh/TGDĐ
        if PHONE_RE.search(f) or ADDR_RE.search(f) or (STORE_RE.search(f) and len(rest.split()) >= 2):
            found.append(f)
    # SĐT cá nhân/cửa hàng trong ảnh mà AI bỏ sót (không tính tổng đài 1800/1900)
    for m in PHONE_RE.finditer(ocr or ""):
        if m.group(0) not in found:
            found.append(m.group(0))
    return found


def check_images(images):
    """Trả về (danh sách ảnh giữ, danh sách (số ảnh, lý do) bị bỏ, cảnh báo)."""
    keep, dropped, warns = [], [], []
    for i, b in enumerate(images):
        try:
            res = ai_call(IMAGE_CHECK_PROMPT, [b], temperature=0)
        except Exception as e:
            print("Không kiểm tra được ảnh", i, e)
            keep.append(b)
            warns.append(f"chưa kiểm tra được ảnh {i + 1}, xem kỹ trước khi đăng")
            continue
        bad = verify_store_info(res.get("ocr", ""), res.get("store_info"))
        print(f"Ảnh {i}: store_info AI={res.get('store_info')} -> xác nhận={bad}")
        if bad:
            dropped.append((i + 1, "; ".join(bad)[:120]))
        else:
            keep.append(b)
    return keep, dropped, warns


# ---------------------------------------------------------------- Apify
def fetch_posts(since):
    url = "https://api.apify.com/v2/acts/apify~facebook-posts-scraper/run-sync-get-dataset-items"
    r = requests.post(url, params={"token": env("APIFY_TOKEN"), "timeout": 280}, timeout=320, json={
        "startUrls": [{"url": u} for u in SOURCE_URLS],
        "resultsLimit": int(os.getenv("RESULTS_LIMIT", "2")),  # 2 bài/trang x 2 trang = tối đa 4 bài/ngày
        "onlyPostsNewerThan": since.strftime("%Y-%m-%d"),
    })
    r.raise_for_status()
    return r.json()


def post_time(p):
    if p.get("timestamp"):
        return datetime.fromtimestamp(int(p["timestamp"]), tz=timezone.utc)
    t = p.get("time")
    if t:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    return None


def image_urls(p):
    urls = []
    for m in p.get("media") or []:
        if not isinstance(m, dict):
            continue
        if m.get("__typename") in ("Video",) or m.get("type") == "video":
            continue
        for key in ("photo_image", "image"):
            v = m.get(key)
            if isinstance(v, dict) and v.get("uri"):
                urls.append(v["uri"])
                break
        else:
            if m.get("thumbnail"):
                urls.append(m["thumbnail"])
    seen, out = set(), []
    for u in urls:
        k = u.split("?")[0]
        if k not in seen:
            seen.add(k)
            out.append(u)
    return out[:10]


def is_video(p):
    return any(isinstance(m, dict) and (m.get("__typename") == "Video" or m.get("type") == "video")
               for m in p.get("media") or []) or bool(p.get("isVideo"))


# ---------------------------------------------------------------- Telegram
def tg(method, **kw):
    files = kw.pop("_files", None)
    r = requests.post(f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/{method}",
                      data=kw if files else None, json=None if files else kw, files=files, timeout=60)
    js = r.json()
    if not js.get("ok"):
        print("Telegram lỗi:", method, js)
    return js


def send_text(text, **kw):
    return tg("sendMessage", chat_id=env("TELEGRAM_CHAT_ID"), text=text[:4000], **kw)


def buttons(pid):
    return {"inline_keyboard": [[{"text": "✅ Đăng", "callback_data": f"ok:{pid}"},
                                 {"text": "❌ Bỏ", "callback_data": f"no:{pid}"}]]}


def send_draft(d, n=None):
    chat = env("TELEGRAM_CHAT_ID")
    imgs = d["images"]
    if len(imgs) == 1:
        with open(ROOT / imgs[0], "rb") as f:
            tg("sendPhoto", chat_id=chat, _files={"photo": f})
    elif len(imgs) > 1:
        files, media = {}, []
        for i, path in enumerate(imgs[:10]):
            files[f"p{i}"] = open(ROOT / path, "rb")
            media.append({"type": "photo", "media": f"attach://p{i}"})
        tg("sendMediaGroup", chat_id=chat, media=json.dumps(media), _files=files)
        for f in files.values():
            f.close()
    head = f"📝 Bài {n} – {d.get('source_page', '')} lúc {d['source_time']}" if n else "📝 Bài đã sửa"
    info = f"Ảnh dùng: {len(imgs)} | ảnh bỏ (có địa chỉ shop khác): {d.get('dropped_images', 0)}"
    for why in d.get("dropped_reasons") or []:
        info += f"\n   – {why}"
    if d.get("warnings"):
        info += "\n⚠️ " + "; ".join(d["warnings"])
    if not imgs:
        info += "\n⚠️ Bài này không còn ảnh nào – bấm Đăng sẽ đăng chữ không."
    msg = f"{head}\n{info}\n➖➖➖➖➖\n{d['text']}\n➖➖➖➖➖\nMuốn sửa: trả lời (reply) tin này bằng nội dung mới."
    js = send_text(msg, reply_markup=buttons(d["id"]), disable_web_page_preview=True)
    if js.get("ok"):
        d["tg_message_id"] = js["result"]["message_id"]
    save_draft(d)


# ---------------------------------------------------------------- Facebook
def fb_publish(d):
    token = env("FB_PAGE_TOKEN")
    if not d["images"]:
        r = requests.post(f"{GRAPH}/me/feed", data={"message": d["text"], "access_token": token}, timeout=60)
    elif len(d["images"]) == 1:
        with open(ROOT / d["images"][0], "rb") as f:
            r = requests.post(f"{GRAPH}/me/photos", data={"caption": d["text"], "access_token": token},
                              files={"source": f}, timeout=120)
    else:
        ids = []
        for path in d["images"]:
            with open(ROOT / path, "rb") as f:
                rr = requests.post(f"{GRAPH}/me/photos", data={"published": "false", "access_token": token},
                                   files={"source": f}, timeout=120)
            rr.raise_for_status()
            ids.append(rr.json()["id"])
        data = {"message": d["text"], "access_token": token}
        for i, mid in enumerate(ids):
            data[f"attached_media[{i}]"] = json.dumps({"media_fbid": mid})
        r = requests.post(f"{GRAPH}/me/feed", data=data, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(r.text[:500])
    js = r.json()
    pid = js.get("post_id") or js.get("id")
    return f"https://www.facebook.com/{pid}"


# ---------------------------------------------------------------- lệnh fetch
def cmd_fetch():
    st = load_state()
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=LOOKBACK_HOURS)
    try:
        items = fetch_posts(since)
    except Exception as e:
        msg = str(e)
        if "402" in msg:
            msg = "Apify đã hết tiền miễn phí trong tháng (402 Payment Required). Vào console.apify.com → Billing để xem ngày làm mới hoặc nạp thêm."
        send_text("⚠️ Sáng nay lấy bài bị lỗi, chưa có bài mới:\n" + msg[:500])
        raise
    print(f"Apify trả về {len(items)} bài")
    new = []
    for p in items:
        pid = str(p.get("postId") or p.get("id") or hashlib.md5(p.get("url", "").encode()).hexdigest()[:16])
        t = post_time(p)
        if pid in st["seen"] or (t and t < since):
            continue
        new.append((t or now, pid, p))
    new.sort(key=lambda x: x[0])
    new = new[-MAX_PER_DAY:]  # chỉ giữ tối đa 4 bài mới nhất

    n = 0
    for t, pid, p in new:
        st["seen"].append(pid)
        text = (p.get("text") or "").strip()
        if is_video(p) and not image_urls(p):
            print("Bỏ bài video:", pid)
            continue
        urls = image_urls(p)
        imgs = []
        for u in urls:
            try:
                rr = requests.get(u, timeout=60)
                if rr.ok and len(rr.content) > 5000:
                    imgs.append(rr.content)
            except Exception as e:
                print("Không tải được ảnh:", e)
        res = ai_rewrite(text, imgs)
        if not res.get("is_sale_post"):
            print("Bỏ bài không phải bán hàng:", pid, res.get("reason"))
            continue
        keep, dropped, img_warns = check_images(imgs)
        final, warns = normalize_post(res.get("post", ""))
        if warns:  # nhờ AI viết ngắn lại 1 lần
            res2 = ai_rewrite(text, keep[:3], extra="\n\nLƯU Ý: lần trước bài quá dài. Viết NGẮN hơn: tối đa 4 dòng, 220 ký tự.")
            final2, warns2 = normalize_post(res2.get("post", ""))
            if len(warns2) <= len(warns):
                final, warns = final2, warns2
        warns = warns + img_warns
        paths = []
        DRAFT_DIR.mkdir(exist_ok=True)
        for i, b in enumerate(keep):
            rel = f"drafts/{pid}_{i}.jpg"
            (ROOT / rel).write_bytes(b)
            paths.append(rel)
        n += 1
        d = {"id": pid, "source_page": requests.utils.unquote(p.get("pageName") or "").replace("-", " "), "source_url": p.get("url"), "source_time": t.astimezone(VN_TZ).strftime("%H:%M %d/%m"),
             "text": final, "warnings": warns, "images": paths, "dropped_images": len(dropped),
             "dropped_reasons": [f"ảnh {k}: «{why}»" for k, why in dropped]}
        save_draft(d)
        send_draft(d, n)
    save_state(st)
    if n == 0:
        send_text("☀️ Sáng nay trang nguồn không có bài bán hàng mới.")
    else:
        send_text(f"☝️ Có {n} bài chờ duyệt. Bấm ✅ Đăng hoặc ❌ Bỏ dưới mỗi bài, bot phản hồi ngay (8h–23h).")


# ---------------------------------------------------------------- lệnh approve
def cmd_approve():
    st = load_state()
    chat = str(env("TELEGRAM_CHAT_ID"))
    js = tg("getUpdates", offset=st.get("tg_offset", 0), timeout=0,
            allowed_updates=["message", "callback_query"])
    for u in js.get("result", []):
        st["tg_offset"] = u["update_id"] + 1
        try:
            if "callback_query" in u:
                cq = u["callback_query"]
                if str(cq["message"]["chat"]["id"]) != chat:
                    continue
                action, pid = cq["data"].split(":", 1)
                d = load_draft(pid)
                if not d:
                    tg("answerCallbackQuery", callback_query_id=cq["id"], text="Bài này đã xử lý rồi")
                    continue
                if action == "ok":
                    link = fb_publish(d)
                    tg("editMessageReplyMarkup", chat_id=chat, message_id=cq["message"]["message_id"],
                       reply_markup={"inline_keyboard": []})
                    send_text(f"✅ Đã đăng: {link}", reply_to_message_id=cq["message"]["message_id"])
                else:
                    tg("editMessageReplyMarkup", chat_id=chat, message_id=cq["message"]["message_id"],
                       reply_markup={"inline_keyboard": []})
                    send_text("❌ Đã bỏ bài này.", reply_to_message_id=cq["message"]["message_id"])
                delete_draft(d)
                tg("answerCallbackQuery", callback_query_id=cq["id"])
            elif "message" in u:
                m = u["message"]
                if str(m["chat"]["id"]) != chat or not m.get("text"):
                    continue
                rep = m.get("reply_to_message")
                if not rep:
                    continue
                d = next((load_draft(p.stem) for p in DRAFT_DIR.glob("*.json")
                          if load_draft(p.stem).get("tg_message_id") == rep["message_id"]), None) if DRAFT_DIR.exists() else None
                if not d:
                    send_text("Không tìm thấy bài nháp tương ứng (có thể đã đăng/bỏ).")
                    continue
                d["text"], d["warnings"] = normalize_post(m["text"])
                tg("editMessageReplyMarkup", chat_id=chat, message_id=rep["message_id"],
                   reply_markup={"inline_keyboard": []})
                send_draft(d)
        except Exception as e:
            print("Lỗi xử lý:", e)
            send_text(f"⚠️ Lỗi khi xử lý: {str(e)[:300]}")
    save_state(st)


def cmd_test_ai():
    sample = ("NHỎ GỌN – CÔNG NGHỆ ĐỈNH – GIÁ CỰC HỜI!\nDJI Osmo Nano (128GB) – Camera kỹ thuật số nhỏ gọn, "
              "lưu giữ mọi khoảnh khắc theo cách ấn tượng!\nGIÁ CHỈ 8.460.000Đ\nTrả góp 0% lãi suất\n"
              "Sắm ngay hôm nay tại Điện Máy Xanh Điện Thắng Bắc!")
    res = ai_rewrite(sample, [])
    print(json.dumps(res, ensure_ascii=False, indent=1))
    text, warns = normalize_post(res.get("post", ""))
    print("\n" + text + f"\n\n({len(text)} ký tự, {len(text.splitlines())} dòng) {warns}")


# ---------------------------------------------------------------- xử lý nút bấm (phản hồi ngay, chặn trùng)
def _set_buttons(chat, mid, rows):
    tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": rows})


def handle_update(u, chat):
    if "callback_query" in u:
        cq = u["callback_query"]
        if str(cq["message"]["chat"]["id"]) != chat:
            return
        mid = cq["message"]["message_id"]
        action, pid = (cq.get("data") or "x:x").split(":", 1)
        if action not in ("ok", "no"):
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Bài này đang/đã được xử lý")
            return
        d = load_draft(pid)
        if not d:
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Bài này đã xử lý rồi")
            return
        if action == "ok":
            _set_buttons(chat, mid, [[{"text": "⏳ Đã nhận – đang đăng…", "callback_data": f"wait:{pid}"}]])
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Đã nhận lệnh đăng")
            try:
                link = fb_publish(d)
            except Exception as e:
                _set_buttons(chat, mid, buttons(pid)["inline_keyboard"])
                send_text(f"⚠️ Đăng lỗi, thử bấm Đăng lại: {str(e)[:300]}", reply_to_message_id=mid)
                return
            _set_buttons(chat, mid, [[{"text": "✅ Đã đăng – mở bài", "url": link}]])
            send_text(f"✅ Đã đăng: {link}", reply_to_message_id=mid)
        else:
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Đã bỏ bài")
            _set_buttons(chat, mid, [[{"text": "❌ Đã bỏ", "callback_data": f"done:{pid}"}]])
        delete_draft(d)
    elif "message" in u:
        m = u["message"]
        if str(m["chat"]["id"]) != chat or not m.get("text"):
            return
        rep = m.get("reply_to_message")
        if not rep or not DRAFT_DIR.exists():
            return
        d = None
        for p in DRAFT_DIR.glob("*.json"):
            x = load_draft(p.stem)
            if x and x.get("tg_message_id") == rep["message_id"]:
                d = x
                break
        if not d:
            send_text("Không tìm thấy bài nháp tương ứng (có thể đã đăng/bỏ).")
            return
        d["text"], d["warnings"] = normalize_post(m["text"])
        _set_buttons(chat, rep["message_id"], [[{"text": "✏️ Đã sửa – xem bản mới bên dưới", "callback_data": f"done:{d['id']}"}]])
        send_draft(d)



# ---------------------------------------------------------------- chuyển tiếp ntfy cho web điểm danh 4543
# Apps Script bị ntfy.sh chặn (IP Google dùng chung, hết hạn mức) -> xếp tin vào hàng chờ,
# bot này (IP riêng của GitHub) lấy hàng chờ mỗi ~45 giây và gửi giúp.
RELAY_URL = os.getenv("NTFY_RELAY_URL", "")
_relay_last = 0.0


def relay_ntfy(force=False):
    global _relay_last
    if not RELAY_URL or (not force and time.time() - _relay_last < 45):
        return
    _relay_last = time.time()
    try:
        r = requests.get(RELAY_URL, params={"api": "q"}, timeout=60)
        js = r.json()
        items, kha = js.get("items", []), js.get("kha", [])
    except Exception as e:
        print("Relay: không lấy được hàng chờ:", e)
        return
    for text in kha:  # báo riêng cho Kha qua Telegram (kết quả lấy bài share...)
        try:
            send_text("📋 Web điểm danh 4543\n" + text, disable_web_page_preview=True)
        except Exception as e:
            print("Relay Telegram lỗi:", e)
    for it in items:
        for attempt in range(3):
            try:
                rr = requests.post("https://ntfy.sh/", json=it, timeout=30)
                print("Relay ntfy:", rr.status_code, it.get("title"))
                if rr.ok:
                    break
            except Exception as e:
                print("Relay ntfy lỗi:", e)
            time.sleep(5)


_posts_last = 0.0


def push_fb_posts(force=False):
    """Lấy các bài mới nhất của fanpage shop (bằng token trang có sẵn) và gửi sang web điểm danh, ~10 phút/lần.
    Web điểm danh dùng danh sách này lúc 9h (ca sáng) và 13h (ca chiều)."""
    global _posts_last
    tok = os.getenv("FB_PAGE_TOKEN", "")
    if not RELAY_URL or not tok or (not force and time.time() - _posts_last < 600):
        return
    _posts_last = time.time()
    try:
        r = requests.get(f"{GRAPH}/me/posts", params={"fields": "message,permalink_url", "limit": 10, "access_token": tok}, timeout=60)
        data = r.json().get("data")
        if data is None:
            print("Lấy bài fanpage lỗi:", r.text[:300])
            return
        posts = [{"t": ((p.get("message") or "Bài đăng").strip().split("\n")[0])[:60], "u": p["permalink_url"]}
                 for p in data if p.get("permalink_url")]
        rr = requests.post(RELAY_URL, json={"api": "posts", "posts": posts}, timeout=60)
        print("Gửi bài cho web điểm danh:", rr.status_code, rr.text[:120])
    except Exception as e:
        print("push_fb_posts lỗi:", e)

TG_FILE = ROOT / "tg_state.json"


def _git(*args):
    import subprocess
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def git_sync(msg=None):
    """GIT_SYNC=1: kéo bản mới nhất (bài nháp mới) trước khi xử lý; có msg thì commit + push sau khi xử lý."""
    if os.getenv("GIT_SYNC") != "1":
        return
    if msg:
        _git("add", "-A")
        if _git("diff", "--cached", "--quiet").returncode != 0:
            _git("commit", "-q", "-m", msg)
    for _ in range(3):
        _git("pull", "--rebase", "-q", "origin", "main")
        if not msg:
            return
        if _git("push", "-q", "origin", "HEAD:main").returncode == 0:
            return
        time.sleep(3)


def load_offset():
    if TG_FILE.exists():
        return json.loads(TG_FILE.read_text(encoding="utf-8")).get("tg_offset", 0)
    return load_state().get("tg_offset", 0)


def save_offset(off):
    TG_FILE.write_text(json.dumps({"tg_offset": off}), encoding="utf-8")


def listen_minutes():
    until = int(os.getenv("LISTEN_UNTIL") or "23")
    now = datetime.now(VN_TZ)
    stop = now.replace(hour=until, minute=0, second=0, microsecond=0)
    return max(0.0, min(340.0, (stop - now).total_seconds() / 60))


def cmd_poll(minutes):
    chat = str(env("TELEGRAM_CHAT_ID"))
    off = load_offset()
    end = time.time() + minutes * 60
    print(f"Trực nút trong {minutes:.0f} phút")
    while True:
        push_fb_posts()
        relay_ntfy()
        wait = int(max(0, min(40, end - time.time())))
        js = tg("getUpdates", offset=off, timeout=wait, allowed_updates=["message", "callback_query"])
        res = js.get("result", [])
        if res:
            print("Telegram:", [(u.get("callback_query") or {}).get("data") or "tin nhắn" for u in res])
            git_sync()
        for u in res:
            off = u["update_id"] + 1
            save_offset(off)
            try:
                handle_update(u, chat)
            except Exception as e:
                print("Lỗi xử lý:", e)
                send_text(f"⚠️ Lỗi khi xử lý: {str(e)[:300]}")
        if res:
            git_sync(f"duyet {datetime.now(VN_TZ):%d/%m %H:%M}")
        if not js.get("ok"):
            time.sleep(10)
        if time.time() >= end:
            break
    save_offset(off)


if __name__ == "__main__":
    {"fetch": cmd_fetch, "approve": lambda: cmd_poll(0), "listen": lambda: cmd_poll(listen_minutes()), "test-ai": cmd_test_ai, "relay": lambda: (push_fb_posts(True), relay_ntfy(True))}[sys.argv[1] if len(sys.argv) > 1 else "fetch"]()
