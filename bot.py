import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from aiocache import Cache
from aiocache.serializers import PickleSerializer
from aiohttp import web
from spotipy import Spotify, SpotifyClientCredentials
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputFile,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)
from savify import Savify, Quality, Format
from savify.utils import PathHolder


BOT_TOKEN = os.environ.get("BOT_TOKEN")
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")
if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
    raise RuntimeError("Spotify credentials not set")
TOKEN: str = BOT_TOKEN

os.environ.setdefault("SPOTIPY_CLIENT_ID", SPOTIFY_CLIENT_ID)
os.environ.setdefault("SPOTIPY_CLIENT_SECRET", SPOTIFY_CLIENT_SECRET)

DOWNLOAD_DIR = Path("/app/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# caches
search_cache = Cache(Cache.MEMORY, serializer=PickleSerializer())
download_cache = Cache(Cache.MEMORY, serializer=PickleSerializer())


@dataclass
class SpotifyItem:
    id: str
    title: str
    artists: str
    type: str


@dataclass
class DownloadJob:
    chat_id: int
    spotify_id: str


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: Dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if record.exc_info:
            data["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(data)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


async def search_spotify(sp: Spotify, user_id: int, query: str) -> List[SpotifyItem]:
    cache_key = f"search:{user_id}:{query}"
    cached = await search_cache.get(cache_key)
    if isinstance(cached, list):
        return cached
    results: List[SpotifyItem] = []
    data = sp.search(query, limit=10, type="track")
    for item in data.get("tracks", {}).get("items", []):
        artists = ", ".join(a["name"] for a in item["artists"])
        results.append(
            SpotifyItem(
                id=item["id"],
                title=item["name"],
                artists=artists,
                type="track",
            )
        )
    await search_cache.set(cache_key, results, ttl=300)
    return results


def build_keyboard(items: List[SpotifyItem]) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text=f"\U0001F3B5 {i.title} – {i.artists}", callback_data=i.id
            )
        ]
        for i in items
    ]
    return InlineKeyboardMarkup(buttons)


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    query = update.message.text or ""
    user = update.effective_user
    if user is None:
        return
    items = await search_spotify(context.application.bot_data["sp"], user.id, query)
    if not items:
        await update.message.reply_text("No results found")
        return
    await update.message.reply_text(
        "Select a track:", reply_markup=build_keyboard(items)
    )


async def handle_inline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.inline_query:
        return
    query = update.inline_query.query or ""
    user = update.inline_query.from_user
    if user is None:
        return
    items = await search_spotify(context.application.bot_data["sp"], user.id, query)
    results = []
    bot_username = context.bot.username
    for i in items:
        token = base64.urlsafe_b64encode(i.id.encode()).decode().rstrip("=")
        url = f"https://t.me/{bot_username}?start={token}"
        results.append(
            InlineQueryResultArticle(
                id=i.id,
                title=f"{i.title} – {i.artists}",
                input_message_content=InputTextMessageContent(
                    f"Downloading {i.title} – {i.artists}"
                ),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Download \u2B07\uFE0F", url=url)]]
                ),
            )
        )
    await update.inline_query.answer(results, cache_time=300)


async def queue_download(
    update: Update, context: ContextTypes.DEFAULT_TYPE, spotify_id: str
) -> None:
    queue: asyncio.Queue[DownloadJob] = context.application.bot_data["queue"]
    chat = update.effective_chat
    if chat is None:
        return
    job = DownloadJob(chat_id=chat.id, spotify_id=spotify_id)
    try:
        queue.put_nowait(job)
        if update.effective_message:
            await update.effective_message.reply_text("Queued for download")
    except asyncio.QueueFull:
        if update.effective_message:
            await update.effective_message.reply_text("Queue full, try later")


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query:
        return
    await update.callback_query.answer()
    spotify_id = update.callback_query.data
    if spotify_id is None:
        return
    await queue_download(update, context, spotify_id)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    token = "".join(context.args or [])
    if token:
        try:
            spotify_id = base64.urlsafe_b64decode(token + "==").decode()
            await queue_download(update, context, spotify_id)
            return
        except Exception:  # noqa: BLE001
            if update.message:
                await update.message.reply_text("Invalid token")
            return
    if update.message:
        await update.message.reply_text("Send me a song name or Spotify link")


async def worker(app: Application[ContextTypes.DEFAULT_TYPE]) -> None:
    queue: asyncio.Queue[DownloadJob] = app.bot_data["queue"]
    sp: Spotify = app.bot_data["sp"]
    while True:
        job = await queue.get()
        try:
            await process_download(app, sp, job)
        except Exception:  # noqa: BLE001
            logging.exception("download error")
        finally:
            queue.task_done()


async def process_download(app: Application[ContextTypes.DEFAULT_TYPE], sp: Spotify, job: DownloadJob) -> None:
    cache_key = f"dl:{job.spotify_id}"
    cached_path = await download_cache.get(cache_key)
    if cached_path and Path(cached_path).is_file():
        path = Path(cached_path)
    else:
        url = f"https://open.spotify.com/track/{job.spotify_id}"
        sav = Savify(
            quality=Quality.BEST,
            download_format=Format.MP3,
            group="%artist%/%album%",
            path_holder=PathHolder(data_path="/app", downloads_path="/app/downloads"),
            ffmpeg_location="ffmpeg",
        )
        await asyncio.to_thread(sav.download, url)
        pattern = next(DOWNLOAD_DIR.rglob(f"{job.spotify_id}*.mp3"), None)
        if not pattern:
            logging.error("file not found after download: %s", job.spotify_id)
            return
        path = pattern
        await download_cache.set(cache_key, str(path), ttl=86400)
    await app.bot.send_audio(
        chat_id=job.chat_id,
        audio=InputFile(str(path)),
        caption=f"https://t.me/{app.bot.username}?start="
        f"{base64.urlsafe_b64encode(job.spotify_id.encode()).decode().rstrip('=')}",
        parse_mode=ParseMode.HTML,
    )


async def main() -> None:
    configure_logging()
    sp = Spotify(auth_manager=SpotifyClientCredentials())
    application = (
        Application.builder()
        .token(TOKEN)
        .rate_limiter(AIORateLimiter())
        .concurrent_updates(True)
        .build()
    )
    application.bot_data["sp"] = sp
    application.bot_data["queue"] = asyncio.LifoQueue(maxsize=10)
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(InlineQueryHandler(handle_inline))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))

    for _ in range(3):
        application.create_task(worker(application))

    # healthz
    async def health(_: web.Request) -> web.Response:
        return web.Response(text="OK")

    runner = web.AppRunner(web.Application())
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    runner.app.router.add_get("/healthz", health)
    await site.start()

    await application.run_polling()
    return


if __name__ == "__main__":
    asyncio.run(main())
