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

async def get_best_url(pid: str) -> tuple[str, int]:
    """Thử các size, trả về (url_lớn_nhất, size_bytes)"""
    sizes = ["orj1080", "mw2000", "orj480", "large", "orj360"]
    best_url = ""
    best_size = 0

    async with httpx.AsyncClient(headers=HEADERS_IMG, follow_redirects=True) as client:
        for s in sizes:
            url = f"https://wx2.sinaimg.cn/{s}/{pid}.jpg"
            try:
                resp = await client.head(url, timeout=8)
                if resp.status_code == 200:
                    size = int(resp.headers.get("content-length", 0))
                    print(f"[SIZE] {s}/{pid[:16]}... → {size} bytes")
                    if size > best_size:
                        best_size = size
                        best_url = url
            except:
                pass

    print(f"[BEST] {best_url.split('/')[3]} → {best_size} bytes")
    return best_url, best_size

async def get_raw_images(url: str) -> tuple[list[str], list[str], list[int]]:
    """Trả về (thumb_urls, raw_urls, raw_sizes)"""
    post_id = extract_weibo_id(url)
    if not post_id:
        print(f"[Scraper] Không extract được post_id từ: {url}")
        return [], [], []

    print(f"[Scraper] post_id: {post_id}")
    thumb_urls = []
    raw_urls = []
    raw_sizes = []
    api_url = f"https://m.weibo.cn/statuses/show?id={post_id}"

    async with httpx.AsyncClient(headers=HEADERS_API, follow_redirects=True) as client:
        try:
            resp = await client.get(api_url, timeout=15)
            data = resp.json()
            pics = data.get("data", {}).get("pics", [])

            # Check size tất cả ảnh song song
            tasks = []
            for pic in pics:
                pid = pic.get("pid", "")
                thumb = pic.get("url", "")
                thumb_urls.append(thumb)
                if pid:
                    tasks.append(get_best_url(pid))
                else:
                    # fallback không có pid
                    fallback = pic.get("large", {}).get("url") or pic.get("url", "")
                    tasks.append(asyncio.coroutine(lambda u=fallback: (u, 0))())

            results = await asyncio.gather(*tasks)
            for raw_url, size in results:
                raw_urls.append(raw_url)
                raw_sizes.append(size)

            print(f"[Scraper] {len(raw_urls)} ảnh, sizes: {[format_size(s) for s in raw_sizes]}")

        except Exception as e:
            print(f"[Scraper Error] {e}")
            import traceback
            traceback.print_exc()

    return thumb_urls, raw_urls, raw_sizes

def get_filename_from_url(url: str) -> str:
    """Lấy tên file gốc từ URL sinaimg"""
    # URL dạng: https://wx2.sinaimg.cn/orj1080/008fk9Edly1ie2udj0hzxj313d1y01ky.jpg
    match = re.search(r"/([^/]+\.(?:jpg|jpeg|png|gif|webp))$", url, re.IGNORECASE)
    if match:
        return match.group(1)
    return "weibo_image.jpg"

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

    thumb_urls, raw_urls, raw_sizes = await get_raw_images(url)
    if not raw_urls:
        await msg.edit_text("❌ Không tìm thấy ảnh nào.")
        return

    await msg.edit_text(f"📥 Đang tải preview {len(raw_urls)} ảnh...")

    thumb_bytes = await asyncio.gather(*[download_image(u) for u in thumb_urls])

    # Lưu session
    session_store[chat_id] = {
        "images": raw_urls,
        "sizes": raw_sizes,
    }

    await msg.edit_text(f"🖼 {len(raw_urls)} ảnh — bấm nút bên dưới mỗi ảnh để tải:")

    # Gửi từng ảnh riêng + button ngay bên dưới
    for i, (thumb, size) in enumerate(zip(thumb_bytes, raw_sizes)):
        if not thumb:
            continue
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"⬇️ #{i+1} — {format_size(size)}",
                callback_data=f"dl_one_{i}"
            )
        ]])
        await update.message.reply_photo(
            photo=io.BytesIO(thumb),
            reply_markup=keyboard
        )
        await asyncio.sleep(0.3)

    # Nút Download All ở cuối
    keyboard_all = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"⬇️ Download All ({len(raw_urls)} ảnh — {format_size(sum(raw_sizes))})",
            callback_data="dl_all"
        )
    ]])
    await update.message.reply_text(
        "👇 Hoặc tải tất cả:",
        reply_markup=keyboard_all
    )

# ─── CALLBACK ─────────────────────────────────────────────────────────────────

async def send_as_file(message, data: bytes, filename: str, caption: str = ""):
    """Gửi ảnh dạng file document — tải về máy trực tiếp, không giới hạn 10MB"""
    await message.reply_document(
        document=io.BytesIO(data),
        filename=filename,
        caption=caption,
        parse_mode="Markdown"
    )

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

        all_bytes = await asyncio.gather(*[download_image(u) for u in images])
        success = 0
        for i, (img_bytes, size) in enumerate(zip(all_bytes, sizes)):
            if img_bytes:
                await send_as_file(
                    query.message,
                    img_bytes,
                    filename=get_filename_from_url(images[i]),
                    caption=f"#{i+1} — {format_size(size)}"
                )
                success += 1
                await asyncio.sleep(0.5)

        await query.message.reply_text(f"✅ Hoàn tất: {success}/{len(images)} ảnh")

    elif data.startswith("dl_one_"):
        idx = int(data.replace("dl_one_", ""))
        if idx >= len(images):
            await query.message.reply_text("❌ Index không hợp lệ.")
            return

        await query.message.reply_text(f"⬇️ Đang tải ảnh #{idx+1}...")
        img_bytes = await download_image(images[idx])
        if img_bytes:
            await send_as_file(
                query.message,
                img_bytes,
                filename=get_filename_from_url(images[idx]),
                caption=f"#{idx+1} — {format_size(sizes[idx])}"  # bỏ URL
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
    _, raw_urls, raw_sizes = await get_raw_images(url)

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
