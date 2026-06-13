# bot.py
# pip install python-telegram-bot httpx beautifulsoup4

import os
import re
import io
import httpx
import asyncio
from telegram import Update, InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

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

# { chat_id: { "images": [...raw_url], "sizes": [...int] } }
session_store: dict = {}

# ─── SCRAPER ──────────────────────────────────────────────────────────────────

def extract_weibo_id(url: str) -> str | None:
    patterns = [
        r"weibo\.com/\d+/(\w+)",
        r"weibo\.com/detail/(\w+)",
        r"m\.weibo\.cn/detail/(\w+)",
        r"m\.weibo\.cn/\d+/(\w+)",
        r"m\.weibo\.cn/status/(\w+)",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

async def get_raw_images(url: str) -> tuple[list[str], list[str]]:
    post_id = extract_weibo_id(url)
    if not post_id:
        print(f"[Scraper] Không extract được post_id từ: {url}")
        return [], []

    print(f"[Scraper] post_id: {post_id}")
    thumb_urls = []
    raw_urls = []
    api_url = f"https://m.weibo.cn/statuses/show?id={post_id}"

    async with httpx.AsyncClient(headers=HEADERS_API, follow_redirects=True) as client:
        try:
            resp = await client.get(api_url, timeout=15)
            data = resp.json()
            pics = data.get("data", {}).get("pics", [])

            # DEBUG: in toàn bộ structure của pic đầu tiên
            if pics:
                import json
                print(f"[DEBUG] pic[0] full structure:")
                print(json.dumps(pics[0], indent=2, ensure_ascii=False))

            for pic in pics:
                thumb = pic.get("url", "")  # orj360 — nhỏ, dùng làm preview
            
                # Lấy URL gốc từ pid — orj1080 là size lớn nhất public
                pid = pic.get("pid", "")
                if pid:
                    raw = f"https://wx2.sinaimg.cn/orj1080/{pid}.jpg"
                else:
                    # fallback về large nếu không có pid
                    raw = (
                        pic.get("large", {}).get("url") or
                        pic.get("url", "")
                    )

                if thumb:
                    thumb_urls.append(thumb)
                if raw:
                    raw_urls.append(raw)

        except Exception as e:
            print(f"[Scraper Error] {e}")

    return thumb_urls, raw_urls

async def download_image(url: str) -> bytes | None:
    async with httpx.AsyncClient(headers=HEADERS_IMG, follow_redirects=True) as client:
        try:
            resp = await client.get(url, timeout=20)
            if resp.status_code == 200:
                return resp.content
            elif resp.status_code == 403:
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

async def get_image_size(url: str) -> int:
    async with httpx.AsyncClient(headers=HEADERS_IMG, follow_redirects=True) as client:
        try:
            resp = await client.head(url, timeout=10)
            return int(resp.headers.get("content-length", 0))
        except:
            return 0

def format_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "?"
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f}MB"
    return f"{size_bytes / 1024:.0f}KB"

# ─── CORE: preview + keyboard ─────────────────────────────────────────────────

async def show_preview(update: Update, url: str):
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("🔍 Đang scrape...")

    thumb_urls, raw_urls = await get_raw_images(url)
    if not raw_urls:
        await msg.edit_text("❌ Không tìm thấy ảnh nào.")
        return

    await msg.edit_text(f"📥 Đang tải preview {len(raw_urls)} ảnh...")

    # Tải thumb + lấy size raw song song
    thumb_bytes, sizes = await asyncio.gather(
        asyncio.gather(*[download_image(u) for u in thumb_urls]),
        asyncio.gather(*[get_image_size(u) for u in raw_urls]),
    )

    # Lưu session
    session_store[chat_id] = {
        "images": raw_urls,
        "sizes": list(sizes),
    }

    # Gửi album preview bằng thumb
    await msg.edit_text(f"🖼 {len(raw_urls)} ảnh — chọn để tải:")

    valid_thumbs = [(i, b) for i, b in enumerate(thumb_bytes) if b]
    chunks = [valid_thumbs[i:i+10] for i in range(0, len(valid_thumbs), 10)]
    for chunk in chunks:
        media_group = [InputMediaPhoto(media=io.BytesIO(b)) for (i, b) in chunk]
        await update.message.reply_media_group(media=media_group)
        await asyncio.sleep(0.5)

    # Inline keyboard
    keyboard = [[
        InlineKeyboardButton(
            f"⬇️ Download All ({len(raw_urls)} ảnh)",
            callback_data="dl_all"
        )
    ]]
    row = []
    for i, size in enumerate(sizes):
        row.append(InlineKeyboardButton(
            f"#{i+1} {format_size(size)}",
            callback_data=f"dl_one_{i}"
        ))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await update.message.reply_text(
        "👇 Chọn ảnh muốn tải:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ─── CALLBACK ─────────────────────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    data = query.data

    session = session_store.get(chat_id)
    if not session:
        await query.message.reply_text("❌ Session hết hạn, paste link lại.")
        return

    images = session["images"]
    sizes = session["sizes"]

    if data == "dl_all":
        await query.message.reply_text(f"⬇️ Đang tải {len(images)} ảnh raw...")

        all_bytes = await asyncio.gather(*[download_image(u) for u in images])
        media_data = [(i, b) for i, b in enumerate(all_bytes) if b]

        chunks = [media_data[i:i+10] for i in range(0, len(media_data), 10)]
        for chunk in chunks:
            media_group = [
                InputMediaPhoto(
                    media=io.BytesIO(b),
                    caption=f"{len(media_data)} ảnh" if idx == 0 else None
                )
                for idx, (i, b) in enumerate(chunk)
            ]
            await query.message.reply_media_group(media=media_group)
            await asyncio.sleep(1)

        await query.message.reply_text(f"✅ Hoàn tất: {len(media_data)}/{len(images)} ảnh")

    elif data.startswith("dl_one_"):
        idx = int(data.replace("dl_one_", ""))
        if idx >= len(images):
            await query.message.reply_text("❌ Index không hợp lệ.")
            return

        await query.message.reply_text(f"⬇️ Đang tải ảnh #{idx+1}...")
        b = await download_image(images[idx])
        if b:
            await query.message.reply_photo(
                photo=io.BytesIO(b),
                caption=f"#{idx+1} — {format_size(sizes[idx])}\n`{images[idx]}`",
                parse_mode="Markdown"
            )
        else:
            await query.message.reply_text(f"❌ Không tải được ảnh #{idx+1}")

# ─── HANDLERS ─────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🖼 Weibo Image Bot\n\n"
        "Paste link bài post Weibo → bot hiện preview album\n"
        "→ Bấm Download All hoặc chọn từng ảnh\n\n"
        "/links <url> — Chỉ lấy URL raw"
    )

async def cmd_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("❌ Dùng: /links <weibo_url>")
        return

    url = ctx.args[0]
    msg = await update.message.reply_text("🔍 Đang scrape...")
    _, raw_urls = await get_raw_images(url)

    if not raw_urls:
        await msg.edit_text("❌ Không tìm thấy ảnh nào.")
        return

    chunks = [raw_urls[i:i+10] for i in range(0, len(raw_urls), 10)]
    await msg.edit_text(f"✅ Tìm thấy {len(raw_urls)} ảnh:")
    for chunk in chunks:
        text = "\n".join(f"`{u}`" for u in chunk)
        await update.message.reply_text(text, parse_mode="Markdown")

async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if "weibo.com" not in text and "weibo.cn" not in text:
        return
    await show_preview(update, text)

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("links", cmd_links))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    print("Bot đang chạy...")
    app.run_polling()

if __name__ == "__main__":
    main()
