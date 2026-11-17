"""
Tìm các bài bot đã đăng lúc app 4543_BOT còn ở chế độ Phát triển (chỉ admin thấy),
rồi đăng lại (bản mới công khai) và xoá bài cũ.

  python fix_hidden.py list     # chỉ liệt kê, gửi danh sách qua Telegram
  python fix_hidden.py repost   # đăng lại + xoá bài cũ
"""
import os
import sys
from pathlib import Path

import requests

from main import GRAPH, ROOT, env, fb_publish, send_text

APP_ID = "2117656925541109"
CUTOFF = os.getenv("CUTOFF") or "2026-11-17T03:20:00+0000"  # lúc chuyển app sang Chính thức (UTC)
TMP = ROOT / "tmp_fix"


def bot_posts():
    token = env("FB_PAGE_TOKEN")
    fields = ("id,message,created_time,permalink_url,application,is_hidden,is_published,privacy,"
              "attachments{media_type,media,subattachments.limit(20){media_type,media}}")
    url, params, out = f"{GRAPH}/me/posts", {"fields": fields, "limit": 50, "access_token": token}, []
    for _ in range(6):
        r = requests.get(url, params=params, timeout=60)
        js = r.json()
        if "error" in js:
            sys.exit(f"Lỗi Graph API: {js['error']}")
        for p in js.get("data", []):
            app = (p.get("application") or {}).get("id")
            print(p["created_time"], p["id"], "app=", app, "privacy=", (p.get("privacy") or {}).get("description"),
                  "|", (p.get("message") or "")[:50].replace("\n", " "))
            if app == APP_ID and p["created_time"] < CUTOFF:
                out.append(p)
        url, params = js.get("paging", {}).get("next"), None
        if not url:
            break
    return out


def image_srcs(p):
    srcs = []
    for a in (p.get("attachments") or {}).get("data", []):
        subs = (a.get("subattachments") or {}).get("data") or [a]
        for s in subs:
            src = ((s.get("media") or {}).get("image") or {}).get("src")
            if src and s.get("media_type", "photo") == "photo":
                srcs.append(src)
    return srcs


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "list"
    posts = bot_posts()
    lines = [f"{i+1}. {p['created_time'][:16]} – {(p.get('message') or '').splitlines()[0][:60] if p.get('message') else '(không chữ)'}"
             f" ({len(image_srcs(p))} ảnh)" for i, p in enumerate(posts)]
    print(f"\nTìm thấy {len(posts)} bài bot đăng trước khi app chuyển Chính thức:\n" + "\n".join(lines))
    if mode != "repost":
        send_text(f"🔎 Có {len(posts)} bài bot đăng lúc app còn chế độ thử nghiệm (có thể đang bị ẩn):\n" + "\n".join(lines))
        return
    token = env("FB_PAGE_TOKEN")
    TMP.mkdir(exist_ok=True)
    done = []
    for i, p in enumerate(reversed(posts)):  # đăng lại theo thứ tự cũ -> mới
        imgs = []
        for j, src in enumerate(image_srcs(p)):
            path = TMP / f"{p['id']}_{j}.jpg"
            path.write_bytes(requests.get(src, timeout=60).content)
            imgs.append(str(path.relative_to(ROOT)))
        try:
            link = fb_publish({"text": p.get("message") or "", "images": imgs})
        except Exception as e:
            send_text(f"⚠️ Đăng lại lỗi, giữ nguyên bài cũ: {(p.get('message') or '')[:60]}\n{e}")
            continue
        r = requests.delete(f"{GRAPH}/{p['id']}", params={"access_token": token}, timeout=60)
        ok = r.status_code == 200 and r.json().get("success")
        done.append(f"✅ {(p.get('message') or '').splitlines()[0][:50]}\n{link}" + ("" if ok else "\n⚠️ Chưa xoá được bài cũ: " + r.text[:200]))
    send_text(f"♻️ Đã đăng lại công khai {len(done)}/{len(posts)} bài và xoá bài cũ bị ẩn:\n\n" + "\n\n".join(done),
              disable_web_page_preview=True)


if __name__ == "__main__":
    main()
