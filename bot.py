# bot.py
# pip install python-telegram-bot httpx beautifulsoup4

import os
import re
import io
import httpx
import asyncio
from telegram import Update, InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

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
# Tăng timeout và retry cho download_image
async def download_image(url: str, timeout: int = 30, retries: int = 3) -> bytes | None:
    for attempt in range(retries):
        async with httpx.AsyncClient(headers=HEADERS_IMG, follow_redirects=True) as client:
            try:
                resp = await client.get(url, timeout=timeout)
                if resp.status_code == 200:
                    return resp.content
                elif resp.status_code == 403:
                    for sub in ["wx1", "wx2", "wx3", "wx4"]:
                        alt_url = re.sub(r"wx\d\.sinaimg\.cn", f"{sub}.sinaimg.cn", url)
                        if alt_url == url:
                            continue
                        resp2 = await client.get(alt_url, timeout=timeout)
                        if resp2.status_code == 200:
                            return resp2.content
            except (httpx.ReadTimeout, httpx.ConnectTimeout):
                print(f"[Timeout] attempt {attempt+1}/{retries}: {url}")
                await asyncio.sleep(2)
            except Exception as e:
                print(f"[Download Error] {url} — {e}")
                break
    return None

# Fix get >9 ảnh — Weibo tách pics_more nếu bài có nhiều hơn 9 ảnh
async def get_raw_images(url: str) -> tuple[list[str], list[str], list[int]]:
    post_id = extract_weibo_id(url)
    if not post_id:
        print(f"[Scraper] Không extract được post_id từ: {url}")
        return [], [], []

    print(f"[Scraper] post_id: {post_id}")
    thumb_urls = []
    raw_urls = []
    api_url = f"https://m.weibo.cn/statuses/show?id={post_id}"

    async with httpx.AsyncClient(headers=HEADERS_API, follow_redirects=True) as client:
        try:
            resp = await client.get(api_url, timeout=15)
            data = resp.json()
            post_data = data.get("data", {})

            # Gộp pics + pics_more (Weibo tách ra khi >9 ảnh)
            pics = post_data.get("pics", [])
            pics_more = post_data.get("pics_more", [])
            all_pics = pics + pics_more
            print(f"[Scraper] pics: {len(pics)}, pics_more: {len(pics_more)}, total: {len(all_pics)}")

            tasks = []
            for pic in all_pics:
                pid = pic.get("pid", "")
                thumb = pic.get("url", "")
                thumb_urls.append(thumb)
                if pid:
                    tasks.append(get_best_url(pid))
                else:
                    fallback = pic.get("large", {}).get("url") or pic.get("url", "")
                    async def _fallback(u=fallback):
                        return (u, 0)
                    tasks.append(_fallback())

            results = await asyncio.gather(*tasks)
            raw_urls = [u for u, s in results]
            raw_sizes = [s for u, s in results]

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
    for attempt in range(1, 4):
        try:
            await message.reply_document(
                document=io.BytesIO(data),
                filename=filename,
                caption=caption,
                parse_mode="Markdown",
                write_timeout=180,   # override: 3 phút cho file lớn
                read_timeout=60,
                connect_timeout=30,
            )
            return  # thành công, thoát
        except Exception as e:
            print(f"[Upload Error] attempt {attempt}/3 — {filename} — {e}")
            if attempt < 3:
                await asyncio.sleep(3 * attempt)  # backoff: 3s, 6s
            else:
                raise  # đã thử 3 lần, ném lỗi ra ngoài để caller xử lý

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
        await download_and_send_all(query.message, images, sizes)

    elif data.startswith("dl_one_"):
        idx = int(data.replace("dl_one_", ""))
        if idx >= len(images):
            await query.message.reply_text("❌ Index không hợp lệ.")
            return

        await query.message.reply_text(f"⬇️ Đang tải ảnh #{idx+1}...")
        b = await download_image(images[idx])
        if b:
            await send_as_file(
                query.message,
                b,
                filename=get_filename_from_url(images[idx]),
                caption=f"#{idx+1} — {format_size(sizes[idx])}"
            )
        else:
            await query.message.reply_text(f"❌ Không tải được ảnh #{idx+1}")

# ─── HANDLERS ─────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🖼 Weibo Image Bot\n\n"
        "Paste link bài post Weibo → bot hiện preview album\n"
        "→ Bấm Download All hoặc chọn từng ảnh\n\n"
        "/links <url> — Chỉ lấy URL raw\n"
        "/all <url> — Download All Files"
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

async def download_and_send_all(message, raw_urls: list, raw_sizes: list):
    """Tải và gửi tuần tự từng ảnh — tránh OOM và timeout khi gather nhiều file lớn"""
    success = 0
    for i, (img_url, size) in enumerate(zip(raw_urls, raw_sizes)):
        b = await download_image(img_url)
        if b:
            try:
                await send_as_file(
                    message,
                    b,
                    filename=get_filename_from_url(img_url),
                    caption=f"#{i+1} — {format_size(size)}"
                )
                success += 1
            except Exception as e:
                await message.reply_text(f"⚠️ Không upload được ảnh #{i+1}: {e}")
        else:
            await message.reply_text(f"⚠️ Không tải được ảnh #{i+1}: {img_url}")
        await asyncio.sleep(0.8)  # tránh flood Telegram

    await message.reply_text(f"✅ Hoàn tất: {success}/{len(raw_urls)} ảnh")

async def cmd_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("❌ Dùng: /all <weibo_url>")
        return

    url = ctx.args[0]
    msg = await update.message.reply_text("⬇️ Đang xử lý...")

    thumb_urls, raw_urls, raw_sizes = await get_raw_images(url)
    if not raw_urls:
        await msg.edit_text("❌ Không tìm thấy ảnh nào.")
        return

    await msg.edit_text(f"📦 Tìm thấy {len(raw_urls)} ảnh, đang tải...")
    await download_and_send_all(update.message, raw_urls, raw_sizes)

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    # Tăng timeout upload file lớn
    request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=60,
        write_timeout=180,   # 3 phút — đủ cho ảnh ~20MB qua mạng chậm
        connect_timeout=30,
        pool_timeout=30,
    )
    app = ApplicationBuilder().token(BOT_TOKEN).request(request).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("links", cmd_links))
    app.add_handler(CommandHandler("all", cmd_all))      # thêm dòng này
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    print("Bot đang chạy...")
    app.run_polling()

if __name__ == "__main__":
    main()
