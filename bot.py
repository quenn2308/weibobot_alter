# bot.py
# pip install python-telegram-bot requests beautifulsoup4 httpx

import os
import re
import httpx
import asyncio
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = "YOUR_BOT_TOKEN"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://weibo.com/",
    "Cookie": "",  # Thêm cookie Weibo nếu bài post cần đăng nhập
}

# ─── SCRAPER ──────────────────────────────────────────────────────────────────

def extract_weibo_id(url: str) -> str | None:
    """Lấy post ID từ link weibo"""
    patterns = [
        r"weibo\.com/\d+/(\w+)",
        r"weibo\.com/detail/(\w+)",
        r"m\.weibo\.cn/detail/(\w+)",
        r"m\.weibo\.cn/\d+/(\w+)",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

async def get_raw_images(url: str) -> list[str]:
    """Scrape tất cả ảnh raw từ 1 bài post Weibo"""
    post_id = extract_weibo_id(url)
    if not post_id:
        return []

    image_urls = []

    # Thử API mobile trước (dễ parse nhất)
    api_url = f"https://m.weibo.cn/detail/{post_id}"
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        try:
            resp = await client.get(api_url, timeout=15)
            html = resp.text

            # Tìm JSON nhúng trong __wb_data__ hoặc render_data
            json_match = re.search(
                r'"pics"\s*:\s*(\[.*?\])', html, re.DOTALL
            )
            if json_match:
                import json
                pics = json.loads(json_match.group(1))
                for pic in pics:
                    # Ưu tiên large > original > url
                    raw = (
                        pic.get("large", {}).get("url") or
                        pic.get("original", {}).get("url") or
                        pic.get("url", "")
                    )
                    if raw:
                        # Đổi thumbnail → ảnh gốc
                        raw = re.sub(r"/thumb\d+/", "/large/", raw)
                        raw = re.sub(r"thumbnail", "large", raw)
                        image_urls.append(raw)

            # Fallback: BeautifulSoup parse img tag
            if not image_urls:
                soup = BeautifulSoup(html, "html.parser")
                for img in soup.find_all("img"):
                    src = img.get("src", "")
                    if "sinaimg.cn" in src or "weibo.com" in src:
                        src = re.sub(r"/thumb\d+/", "/large/", src)
                        src = re.sub(r"orj\d+", "large", src)
                        image_urls.append(src)

        except Exception as e:
            print(f"[Scraper Error] {e}")

    # Deduplicate giữ thứ tự
    seen = set()
    result = []
    for u in image_urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result

async def download_image(url: str) -> bytes | None:
    """Tải ảnh về dạng bytes"""
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        try:
            resp = await client.get(url, timeout=20)
            if resp.status_code == 200:
                return resp.content
        except Exception as e:
            print(f"[Download Error] {url} — {e}")
    return None

# ─── HANDLERS ─────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🖼 Weibo Image Bot\n\n"
        "Gửi link bài post Weibo, bot sẽ:\n"
        "/links <url> — Gửi danh sách URL ảnh raw\n"
        "/download <url> — Tải và gửi file ảnh\n\n"
        "Hoặc paste link thẳng → tự động gửi URL"
    )

async def cmd_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Trả về danh sách URL ảnh raw"""
    if not ctx.args:
        await update.message.reply_text("❌ Dùng: /links <weibo_url>")
        return

    url = ctx.args[0]
    msg = await update.message.reply_text("🔍 Đang scrape...")

    images = await get_raw_images(url)
    if not images:
        await msg.edit_text("❌ Không tìm thấy ảnh nào. Kiểm tra link hoặc thêm cookie.")
        return

    # Chia thành chunks 10 link/tin nhắn
    chunks = [images[i:i+10] for i in range(0, len(images), 10)]
    await msg.edit_text(f"✅ Tìm thấy {len(images)} ảnh:")
    for chunk in chunks:
        text = "\n".join(f"`{u}`" for u in chunk)
        await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tải ảnh về và gửi file"""
    if not ctx.args:
        await update.message.reply_text("❌ Dùng: /download <weibo_url>")
        return

    url = ctx.args[0]
    msg = await update.message.reply_text("⬇️ Đang tải ảnh...")

    images = await get_raw_images(url)
    if not images:
        await msg.edit_text("❌ Không tìm thấy ảnh nào.")
        return

    await msg.edit_text(f"📦 Đang gửi {len(images)} ảnh...")

    for i, img_url in enumerate(images, 1):
        data = await download_image(img_url)
        if data:
            filename = f"weibo_{i:03d}.jpg"
            await update.message.reply_document(
                document=data,
                filename=filename,
                caption=f"[{i}/{len(images)}] {img_url}"
            )
            await asyncio.sleep(0.5)  # tránh flood
        else:
            await update.message.reply_text(f"⚠️ Không tải được ảnh {i}: {img_url}")

async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Paste link thẳng → tự động trả URL"""
    text = update.message.text or ""
    if "weibo.com" not in text and "weibo.cn" not in text:
        return

    msg = await update.message.reply_text("🔍 Đang scrape...")
    images = await get_raw_images(text.strip())

    if not images:
        await msg.edit_text("❌ Không tìm thấy ảnh. Thêm cookie nếu bài post cần login.")
        return

    chunks = [images[i:i+10] for i in range(0, len(images), 10)]
    await msg.edit_text(
        f"✅ {len(images)} ảnh — dùng /download <url> để tải file:"
    )
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