import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar, cast

from aiocache import Cache
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

from savify import Savify
from savify.logger import Logger
from savify.types import Format, Quality
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

TOKEN = os.environ["BOT_TOKEN"]
CLIENT_ID = os.environ["SPOTIPY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPOTIPY_CLIENT_SECRET"]
REDIS_URL = os.getenv("REDIS_URL")

DOWNLOAD_DIR = Path("/app/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

SEARCH_CACHE = Cache.from_url(REDIS_URL) if REDIS_URL else Cache(Cache.MEMORY)
DOWNLOAD_CACHE = Cache.from_url(REDIS_URL) if REDIS_URL else Cache(Cache.MEMORY)


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


logging.basicConfig(level=logging.INFO)
for handler in logging.getLogger().handlers:
    handler.setFormatter(JsonFormatter())

logger = logging.getLogger("spotify_dl_bot")


@dataclass
class Job:
    chat_id: int
    spotify_type: str
    spotify_id: str


queue: asyncio.LifoQueue = asyncio.LifoQueue(maxsize=30)  # type: ignore[type-arg]


async def spotify_client() -> spotipy.Spotify:
    auth = SpotifyClientCredentials(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    return spotipy.Spotify(auth_manager=auth)


T = TypeVar("T")


async def run_in_executor(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


async def search_spotify(user_id: int, query: str) -> List[Dict[str, str]]:
    cache_key = "search:{}:{}".format(user_id, query)
    cached = await SEARCH_CACHE.get(cache_key)
    if cached:
        return cast(List[Dict[str, str]], cached)
    sp = await spotify_client()
    results = await run_in_executor(sp.search, q=query, limit=10, type="track,album,playlist,artist")
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


SPOTIFY_URL_RE = re.compile(r"open\.spotify\.com/(track|album|playlist|artist)/([A-Za-z0-9]+)")


def parse_spotify_link(text: str) -> Optional[Tuple[str, str]]:
    match = SPOTIFY_URL_RE.search(text)
    if match:
        return match.group(1), match.group(2)
    return None


def create_share_link(username: str, s_type: str, s_id: str) -> str:
    token = base64.urlsafe_b64encode("{}:{}".format(s_type, s_id).encode()).decode().rstrip("=")
    return "https://t.me/{}?start={}".format(username, token)


bot = Bot(token=TOKEN)
dp = Dispatcher(bot)


async def queue_download(chat_id: int, s_type: str, s_id: str) -> None:
    if queue.full():
        await bot.send_message(chat_id, "Too many downloads in progress. Please try later.")
        return
    await queue.put(Job(chat_id, s_type, s_id))
    await bot.send_message(chat_id, "Download queued. Please wait…")


def savify_instance() -> Savify:
    token = Savify.get_token(CLIENT_ID, CLIENT_SECRET)
    return Savify(
        token,
        quality=Quality.BEST,
        download_format=Format.MP3,
        group="%artist%/%album%",
        logger=Logger(level=Logger.CRITICAL),
    )


async def process_job(job: Job) -> None:
    cache_key = "dl:{}:{}:best".format(job.spotify_type, job.spotify_id)
    file_path = DOWNLOAD_DIR / "{}.mp3".format(job.spotify_id)
    cached = await DOWNLOAD_CACHE.get(cache_key)
    if not cached or not file_path.exists():
        url = "https://open.spotify.com/{}/{}".format(job.spotify_type, job.spotify_id)
        sav = await run_in_executor(savify_instance)
        try:
            await run_in_executor(sav.download, url, str(DOWNLOAD_DIR))
        except Exception as exc:  # pragma: no cover
            logger.error("download_failed", exc_info=exc)
            await bot.send_message(job.chat_id, "Download failed")
            return
        await DOWNLOAD_CACHE.set(cache_key, True, ttl=86400)
        for p in DOWNLOAD_DIR.rglob("{}*.mp3".format(job.spotify_id)):
            file_path = p
            break
    me = await bot.get_me()
    share_link = create_share_link(me.username, job.spotify_type, job.spotify_id)
    await bot.send_audio(job.chat_id, audio=types.InputFile(file_path), caption=share_link)


async def worker() -> None:
    while True:
        job = await queue.get()
        try:
            await process_job(job)
        finally:
            queue.task_done()


@dp.message_handler(commands=["start"])  # type: ignore[misc]
async def start_command(message: types.Message) -> None:
    args = message.get_args()
    if args:
        try:
            data = base64.urlsafe_b64decode(args + "=" * (-len(args) % 4)).decode()
            s_type, s_id = data.split(":")
            await queue_download(message.chat.id, s_type, s_id)
            return
        except Exception as exc:  # pragma: no cover
            logger.error("start_decode_failed", exc_info=exc)
    await message.reply("Send me a song name or Spotify link.")


def is_plain_text(message: types.Message) -> bool:
    return bool(message.text and not message.text.startswith("/"))


@dp.message_handler(lambda m: is_plain_text(m))  # type: ignore[misc]
async def text_handler(message: types.Message) -> None:
    text = message.text or ""
    parsed = parse_spotify_link(text)
    if parsed:
        s_type, s_id = parsed
        await queue_download(message.chat.id, s_type, s_id)
        return
    results = await search_spotify(message.from_user.id, text)
    keyboard = [
        [types.InlineKeyboardButton("\U0001F3B5 {} – {}".format(r["title"], r["artist"]), callback_data="{}:{}".format(r["type"], r["id"]))]
        for r in results[:10]
    ]
    await message.reply("Select:", reply_markup=types.InlineKeyboardMarkup(keyboard))


@dp.callback_query_handler()  # type: ignore[misc]
async def button_handler(query: types.CallbackQuery) -> None:
    await query.answer()
    if query.data:
        s_type, s_id = query.data.split(":")
        await queue_download(query.message.chat.id, s_type, s_id)


@dp.inline_handler()  # type: ignore[misc]
async def inline_handler(inline_query: types.InlineQuery) -> None:
    q = inline_query.query
    if not q:
        return
    results = await search_spotify(inline_query.from_user.id, q)
    articles: List[types.InlineQueryResultArticle] = []
    me = await bot.get_me()
    for r in results[:10]:
        token = base64.urlsafe_b64encode("{}:{}".format(r["type"], r["id"]).encode()).decode().rstrip("=")
        link = "https://t.me/{}?start={}".format(me.username, token)
        articles.append(
            types.InlineQueryResultArticle(
                id="{}-{}".format(r["type"], r["id"]),
                title=r["title"],
                description=r["artist"],
                input_message_content=types.InputTextMessageContent(
                    "Click below to download {}".format(r["title"])
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[[types.InlineKeyboardButton("Download \U0001F53D", url=link)]]
                ),
            )
        )
    await inline_query.answer(articles, is_personal=True, cache_time=0)


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def on_startup(dp: Dispatcher) -> None:
    app = web.Application()
    app.router.add_get("/healthz", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    for _ in range(3):
        asyncio.ensure_future(worker())


if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
