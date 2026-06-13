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

# Lưu tạm danh sách ảnh theo chat_id để dùng khi bấm nút
# { chat_id: { "images": [...url], "sizes": [...int] } }
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
            data = resp.json()
            pics = data.get("data", {}).get("pics", [])

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

            print(f"[Scraper] Tìm thấy {len(image_urls)} ảnh")

        except Exception as e:
            print(f"[Scraper Error] {e}")

    return image_urls

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
    """Lấy size ảnh qua HEAD request, trả về bytes"""
    async with httpx.AsyncClient(headers=HEADERS_IMG, follow_redirects=True) as client:
        try:
            resp = await client.head(url, timeout=10)
            size = int(resp.headers.get("content-length", 0))
            return size
        except:
            return 0

def format_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "?"
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    return f"{size_bytes / 1024:.0f} KB"

# ─── CORE: hiện album preview + nút chọn ──────────────────────────────────────

async def show_preview(update: Update, url: str):
    """Scrape → hiện album preview → hiện nút Download All + từng ảnh"""
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("🔍 Đang scrape...")

    images = await get_raw_images(url)
    if not images:
        await msg.edit_text("❌ Không tìm thấy ảnh nào.")
        return

    await msg.edit_text(f"📥 Đang lấy thông tin {len(images)} ảnh...")

    # Lấy size tất cả ảnh song song
    sizes = await asyncio.gather(*[get_image_size(u) for u in images])

    # Lưu session
    session_store[chat_id] = {"images": images, "sizes": list(sizes)}

    # Gửi album preview (tối đa 10 ảnh/group)
    await msg.edit_text(f"🖼 {len(images)} ảnh — chọn để tải:")

    chunks = [images[i:i+10] for i in range(0, len(images), 10)]
    for chunk_idx, chunk in enumerate(chunks):
        media_group = []
        for idx, img_url in enumerate(chunk):
            media_group.append(InputMediaPhoto(media=img_url))
        await update.message.reply_media_group(media=media_group)
        await asyncio.sleep(0.5)

    # Tạo inline keyboard
    keyboard = []

    # Nút Download All
    keyboard.append([
        InlineKeyboardButton(
            f"⬇️ Download All ({len(images)} ảnh)",
            callback_data=f"dl_all"
        )
    ])

    # Nút từng ảnh (mỗi hàng 3 nút)
    row = []
    for i, (img_url, size) in enumerate(zip(images, sizes)):
        row.append(
            InlineKeyboardButton(
                f"#{i+1} {format_size(size)}",
                callback_data=f"dl_one_{i}"
            )
        )
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await update.message.reply_text(
        "👇 Chọn ảnh muốn tải:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ─── CALLBACK: xử lý bấm nút ─────────────────────────────────────────────────

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
        await query.message.reply_text(f"⬇️ Đang tải {len(images)} ảnh...")
        await send_images(query.message, images, sizes)

    elif data.startswith("dl_one_"):
        idx = int(data.replace("dl_one_", ""))
        if idx >= len(images):
            await query.message.reply_text("❌ Index không hợp lệ.")
            return
        img_url = images[idx]
        await query.message.reply_text(f"⬇️ Đang tải ảnh #{idx+1}...")
        data_bytes = await download_image(img_url)
        if data_bytes:
            await query.message.reply_photo(
                photo=io.BytesIO(data_bytes),
                caption=f"#{idx+1} — {format_size(sizes[idx])}\n`{img_url}`",
                parse_mode="Markdown"
            )
        else:
            await query.message.reply_text(f"❌ Không tải được ảnh #{idx+1}")

async def send_images(message, images: list, sizes: list):
    """Tải và gửi tất cả ảnh dạng media group"""
    media_data = []
    for i, img_url in enumerate(images):
        data = await download_image(img_url)
        if data is not None and len(data) > 0:
            media_data.append((i, img_url, data, sizes[i]))
        else:
            print(f"[Skip] Không tải được ảnh {i+1}")

    if not media_data:
        await message.reply_text("❌ Không tải được ảnh nào.")
        return

    chunks = [media_data[i:i+10] for i in range(0, len(media_data), 10)]
    for chunk in chunks:
        media_group = []
        for idx, (i, img_url, data, size) in enumerate(chunk):
            media_group.append(
                InputMediaPhoto(
                    media=io.BytesIO(data),
                    caption=f"{len(media_data)} ảnh — {format_size(sum(sizes))}" if idx == 0 else None
                )
            )
        await message.reply_media_group(media=media_group)
        await asyncio.sleep(1)

    await message.reply_text(f"✅ Hoàn tất: {len(media_data)}/{len(images)} ảnh")

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
    images = await get_raw_images(url)

    if not images:
        await msg.edit_text("❌ Không tìm thấy ảnh nào.")
        return

    chunks = [images[i:i+10] for i in range(0, len(images), 10)]
    await msg.edit_text(f"✅ Tìm thấy {len(images)} ảnh:")
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
