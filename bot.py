# bot.py
# pip install python-telegram-bot httpx beautifulsoup4

import os
import re
import io
import httpx
import asyncio
from telegram import Update, InputMediaPhoto
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN")

HEADERS_API = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
    "Referer": "https://m.weibo.cn/",
    "Accept": "application/json, text/plain, */*",
    "MWeibo-Pwa": "1",
    "X-Requested-With": "XMLHttpRequest",
}

HEADERS_IMG = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://weibo.com/",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
    "Cookie": "",  # thêm cookie nếu cần
}

# ─── SCRAPER ──────────────────────────────────────────────────────────────────

def extract_weibo_id(url: str) -> str | None:
    patterns = [
        r"weibo\.com/\d+/(\w+)",
        r"weibo\.com/detail/(\w+)",
        r"m\.weibo\.cn/detail/(\w+)",
        r"m\.weibo\.cn/\d+/(\w+)",
        r"m\.weibo\.cn/status/(\w+)",   # thêm dòng này
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

async def get_raw_images(url: str) -> list[str]:
    post_id = extract_weibo_id(url)
    if not post_id:
        print(f"[Scraper] Không extract được post_id từ: {url}")
        return []

    print(f"[Scraper] post_id: {post_id}")
    image_urls = []
    api_url = f"https://m.weibo.cn/statuses/show?id={post_id}"

    async with httpx.AsyncClient(headers=HEADERS_API, follow_redirects=True) as client:
        try:
            resp = await client.get(api_url, timeout=15)
            print(f"[Scraper] HTTP status: {resp.status_code}")
            print(f"[Scraper] Response raw: {resp.text[:500]}")  # in 500 ký tự đầu

            data = resp.json()
            print(f"[Scraper] JSON keys: {list(data.keys())}")

            inner = data.get("data", {})
            print(f"[Scraper] data keys: {list(inner.keys()) if isinstance(inner, dict) else type(inner)}")

            pics = inner.get("pics", [])
            print(f"[Scraper] pics count: {len(pics)}")
            if pics:
                print(f"[Scraper] pic[0] keys: {list(pics[0].keys())}")
                print(f"[Scraper] pic[0] sample: {pics[0]}")

            for pic in pics:
                raw = (
                    pic.get("large", {}).get("url") or
                    pic.get("original", {}).get("url") or
                    pic.get("url", "")
                )
                if raw:
                    raw = re.sub(r"/thumb\d+/", "/large/", raw)
                    raw = re.sub(r"orj\d+", "large", raw)
                    image_urls.append(raw)

        except Exception as e:
            print(f"[Scraper Error] {e}")
            import traceback
            traceback.print_exc()

    return image_urls

async def download_image(url: str) -> bytes | None:
    async with httpx.AsyncClient(headers=HEADERS_IMG, follow_redirects=True) as client:
        try:
            resp = await client.get(url, timeout=20)
            print(f"[Download] {url} → status {resp.status_code}")
            if resp.status_code == 200:
                return resp.content
            elif resp.status_code == 403:
                # Thử đổi subdomain sinaimg
                for sub in ["wx1", "wx2", "wx3", "wx4"]:
                    alt_url = re.sub(r"wx\d\.sinaimg\.cn", f"{sub}.sinaimg.cn", url)
                    if alt_url == url:
                        continue
                    resp2 = await client.get(alt_url, timeout=20)
                    if resp2.status_code == 200:
                        return resp2.content
        except Exception as e:
            print(f"[Download Error] {url} — {e}")
    return None

# ─── HANDLERS ─────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🖼 Weibo Image Bot\n\n"
        "Gửi link bài post Weibo, bot sẽ:\n"
        "/links <url> — Danh sách URL ảnh raw\n"
        "/download <url> — Tải và gửi ảnh\n\n"
        "Hoặc paste link thẳng → tự động trả URL"
    )

async def cmd_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("❌ Dùng: /links <weibo_url>")
        return

    url = ctx.args[0]
    msg = await update.message.reply_text("🔍 Đang scrape...")

    images = await get_raw_images(url)
    if not images:
        await msg.edit_text("❌ Không tìm thấy ảnh nào.")
        return

    chunks = [images[i:i+10] for i in range(0, len(images), 10)]
    await msg.edit_text(f"✅ Tìm thấy {len(images)} ảnh:")
    for chunk in chunks:
        text = "\n".join(f"`{u}`" for u in chunk)
        await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("❌ Dùng: /download <weibo_url>")
        return

    url = ctx.args[0]
    msg = await update.message.reply_text("⬇️ Đang tải ảnh...")

    images = await get_raw_images(url)
    if not images:
        await msg.edit_text("❌ Không tìm thấy ảnh nào.")
        return

    await msg.edit_text(f"📦 Tìm thấy {len(images)} ảnh, đang tải...")

    # Tải tất cả ảnh trước
    media_data = []
    for i, img_url in enumerate(images, 1):
        data = await download_image(img_url)
        if data is not None and len(data) > 0:
            media_data.append((i, img_url, data))
        else:
            print(f"[Skip] Không tải được ảnh {i}: {img_url}")

    if not media_data:
        await msg.edit_text("❌ Không tải được ảnh nào.")
        return

    # Gửi theo nhóm media (tối đa 10 ảnh/nhóm)
    chunks = [media_data[i:i+10] for i in range(0, len(media_data), 10)]
    for chunk in chunks:
        media_group = []
        for idx, (i, img_url, data) in enumerate(chunk):
            media_group.append(
                InputMediaPhoto(
                    media=io.BytesIO(data),
                    caption=img_url if idx == 0 else None
                )
            )
        await update.message.reply_media_group(media=media_group)
        await asyncio.sleep(1)

    await msg.edit_text(f"✅ Hoàn tất: {len(media_data)}/{len(images)} ảnh")

async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if "weibo.com" not in text and "weibo.cn" not in text:
        return

    msg = await update.message.reply_text("🔍 Đang scrape...")
    images = await get_raw_images(text.strip())

    if not images:
        await msg.edit_text("❌ Không tìm thấy ảnh. Thử thêm cookie nếu bài post cần login.")
        return

    chunks = [images[i:i+10] for i in range(0, len(images), 10)]
    await msg.edit_text(f"✅ {len(images)} ảnh — dùng /download <url> để tải file:")
    for chunk in chunks:
        text_out = "\n".join(f"`{u}`" for u in chunk)
        await update.message.reply_text(text_out, parse_mode="Markdown")

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("links", cmd_links))
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    print("Bot đang chạy...")
    app.run_polling()

if __name__ == "__main__":
    main()
