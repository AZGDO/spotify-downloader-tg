import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple, cast

from aiocache import Cache
from aiohttp import web
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputFile,
    InputTextMessageContent,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from savify import Savify
from savify.logger import Logger
from savify.types import Format, Quality
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

TOKEN = "7960705872:AAE52ca1zbKizNRE6dCNdUPe_yMp89Nhxwg"
CLIENT_ID = "4d2f985998b1462b97c400b889e1919c"
CLIENT_SECRET = "36a1ccd92f3e48f18e88baa0b8e5fab7"
REDIS_URL = os.getenv("REDIS_URL")

DOWNLOAD_DIR = Path("/app/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

SEARCH_CACHE = Cache.from_url(REDIS_URL) if REDIS_URL else Cache(Cache.MEMORY)
DOWNLOAD_CACHE = Cache.from_url(REDIS_URL) if REDIS_URL else Cache(Cache.MEMORY)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("spotify_dl_bot")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "message": record.getMessage(),
            "name": record.name,
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


for h in logging.getLogger().handlers:
    h.setFormatter(JsonFormatter())


@dataclass
class Job:
    chat_id: int
    spotify_type: str
    spotify_id: str


queue: asyncio.LifoQueue[Job] = asyncio.LifoQueue(maxsize=30)


async def spotify_client() -> spotipy.Spotify:
    auth = SpotifyClientCredentials(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    return spotipy.Spotify(auth_manager=auth)


async def search_spotify(user_id: int, query: str) -> List[Dict[str, str]]:
    cache_key = f"search:{user_id}:{query}"
    cached = await SEARCH_CACHE.get(cache_key)
    if cached:
        return cast(List[Dict[str, str]], cached)
    sp = await spotify_client()
    results = await asyncio.to_thread(
        sp.search, q=query, limit=10, type="track,album,playlist,artist"
    )
    items: List[Dict[str, str]] = []
    for t in results.get("tracks", {}).get("items", []):
        artist = ", ".join(a["name"] for a in t.get("artists", []))
        items.append({"id": t["id"], "type": "track", "title": t["name"], "artist": artist})
    for t in results.get("albums", {}).get("items", []):
        artist = ", ".join(a["name"] for a in t.get("artists", []))
        items.append({"id": t["id"], "type": "album", "title": t["name"], "artist": artist})
    for t in results.get("playlists", {}).get("items", []):
        owner = t.get("owner", {}).get("display_name", "")
        items.append({"id": t["id"], "type": "playlist", "title": t["name"], "artist": owner})
    for t in results.get("artists", {}).get("items", []):
        items.append({"id": t["id"], "type": "artist", "title": t["name"], "artist": t["name"]})
    await SEARCH_CACHE.set(cache_key, items, ttl=300)
    return items


SPOTIFY_URL_RE = re.compile(r"open.spotify.com/(track|album|playlist|artist)/([A-Za-z0-9]+)")


def parse_spotify_link(text: str) -> Tuple[str, str] | None:
    match = SPOTIFY_URL_RE.search(text)
    if match:
        return match.group(1), match.group(2)
    return None


def create_share_link(username: str, s_type: str, s_id: str) -> str:
    token = base64.urlsafe_b64encode(f"{s_type}:{s_id}".encode()).decode().rstrip("=")
    return f"https://t.me/{username}?start={token}"


async def queue_download(chat_id: int, s_type: str, s_id: str, app: Application) -> None:
    if queue.full():
        await app.bot.send_message(chat_id=chat_id, text="Too many downloads in progress. Please try later.")
        return
    await queue.put(Job(chat_id, s_type, s_id))
    await app.bot.send_message(chat_id=chat_id, text="Download queued. Please wait…")


def savify_instance() -> Savify:
    token = Savify.get_token(CLIENT_ID, CLIENT_SECRET)
    return Savify(
        token,
        quality=Quality.BEST,
        download_format=Format.MP3,
        group="%artist%/%album%",
        logger=Logger(level=Logger.CRITICAL),
    )


async def process_job(app: Application, job: Job) -> None:
    cache_key = f"dl:{job.spotify_type}:{job.spotify_id}:best"
    file_path = DOWNLOAD_DIR / f"{job.spotify_id}.mp3"
    cached = await DOWNLOAD_CACHE.get(cache_key)
    if not cached or not file_path.exists():
        url = f"https://open.spotify.com/{job.spotify_type}/{job.spotify_id}"
        sav = await asyncio.to_thread(savify_instance)
        try:
            await asyncio.to_thread(sav.download, url, str(DOWNLOAD_DIR))
        except Exception as exc:  # pragma: no cover
            logger.error("download_failed", exc_info=exc)
            await app.bot.send_message(chat_id=job.chat_id, text="Download failed")
            return
        await DOWNLOAD_CACHE.set(cache_key, True, ttl=86400)
        # find the downloaded file
        for p in DOWNLOAD_DIR.rglob(f"{job.spotify_id}*.mp3"):
            file_path = p
            break
    share_link = create_share_link((await app.bot.get_me()).username, job.spotify_type, job.spotify_id)
    audio = InputFile(file_path)
    await app.bot.send_audio(chat_id=job.chat_id, audio=audio, caption=share_link)


async def worker(app: Application) -> None:
    while True:
        job = await queue.get()
        try:
            await process_job(app, job)
        finally:
            queue.task_done()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        try:
            data = base64.urlsafe_b64decode(context.args[0] + "=" * (-len(context.args[0]) % 4)).decode()
            s_type, s_id = data.split(":")
            await queue_download(update.effective_chat.id, s_type, s_id, context.application)
            return
        except Exception as exc:  # pragma: no cover
            logger.error("start_decode_failed", exc_info=exc)
    await update.message.reply_text("Send me a song name or Spotify link.")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    text = update.message.text or ""
    parsed = parse_spotify_link(text)
    if parsed:
        s_type, s_id = parsed
        await queue_download(update.effective_chat.id, s_type, s_id, context.application)
        return
    results = await search_spotify(update.effective_user.id, text)
    keyboard = [
        [InlineKeyboardButton(f"\U0001F3B5 {r['title']} – {r['artist']}", callback_data=f"{r['type']}:{r['id']}")]
        for r in results[:10]
    ]
    await update.message.reply_text("Select:", reply_markup=InlineKeyboardMarkup(keyboard))


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data:
        s_type, s_id = query.data.split(":")
        await queue_download(query.message.chat_id, s_type, s_id, context.application)


async def inline_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.inline_query.query
    if not q:
        return
    results = await search_spotify(update.inline_query.from_user.id, q)
    articles: List[InlineQueryResultArticle] = []
    bot_username = (await context.bot.get_me()).username
    for r in results[:10]:
        token = base64.urlsafe_b64encode(f"{r['type']}:{r['id']}".encode()).decode().rstrip("=")
        link = f"https://t.me/{bot_username}?start={token}"
        articles.append(
            InlineQueryResultArticle(
                id=f"{r['type']}-{r['id']}",
                title=r['title'],
                description=r['artist'],
                input_message_content=InputTextMessageContent(
                    f"Click below to download {r['title']}"
                ),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Download 🔽", url=link)]]
                ),
            )
        )
    await update.inline_query.answer(articles, is_personal=True, cache_time=0)


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def main() -> None:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(InlineQueryHandler(inline_handler))
    # workers
    for _ in range(3):
        app.job_queue.run_async(worker(app))

    # health endpoint
    runner = web.AppRunner(web.Application())
    runner.app.add_routes([web.get("/healthz", health)])
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await app.updater.idle()


if __name__ == "__main__":
    asyncio.run(main())
