from __future__ import annotations

import asyncio
import base64
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from aiocache import Cache
from fastapi import FastAPI
import uvicorn
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InputFile,
    Message,
    Update,
)
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    ChosenInlineResultHandler,
    MessageHandler,
    filters,
)
import yt_dlp as youtube_dl

sys.modules["youtube_dl"] = youtube_dl  # noqa: E402
from savify import Savify  # noqa: E402
from savify.types import Format, Quality  # noqa: E402
from savify.utils import PathHolder, safe_path_string  # noqa: E402
from savify.track import Track  # noqa: E402
from savify.savify import _sort_dir  # noqa: E402
import spotipy  # noqa: E402
from spotipy.oauth2 import SpotifyClientCredentials  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

SPOTIFY_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

SEARCH_CACHE = Cache(Cache.MEMORY, ttl=300)  # 5 minutes
DOWNLOAD_CACHE = Cache(Cache.MEMORY, ttl=86400)  # 24 hours

DOWNLOAD_DIR = Path("/app/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

QUEUE_MAX_SIZE = 3
queue: asyncio.LifoQueue[Dict[str, Any]] = asyncio.LifoQueue(maxsize=QUEUE_MAX_SIZE)

APPLICATION: Application[Any, Any, Any, Any, Any, Any] | None = None

sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=SPOTIFY_ID, client_secret=SPOTIFY_SECRET))

app = FastAPI()

async def run_web() -> None:
    config = uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


def encode_id(sid: str) -> str:
    return base64.urlsafe_b64encode(sid.encode()).decode().rstrip("=")


def decode_id(token: str) -> str:
    padding = "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(token + padding).decode()


async def compute_download_path(track_id: str) -> Path:
    data: Dict[str, Any] = await asyncio.to_thread(sp.track, track_id)
    track = Track(data)
    group: str = _sort_dir(track, "%artist%/%album%")
    file_name: str = safe_path_string(f"{str(track)}.mp3")
    return DOWNLOAD_DIR / Path(group) / file_name


async def download_track(track_id: str) -> Path:
    """Download a single track and return the resulting file path."""
    url = f"https://open.spotify.com/track/{track_id}"
    savify = Savify(
        api_credentials=(SPOTIFY_ID or "", SPOTIFY_SECRET or ""),
        quality=Quality.BEST,
        download_format=Format.MP3,
        group="%artist%/%album%",
        path_holder=PathHolder(downloads_path=str(DOWNLOAD_DIR)),
        logger=logger,
    )
    await asyncio.to_thread(savify.download, url)
    file_path = await compute_download_path(track_id)
    if not file_path.exists():
        raise FileNotFoundError(str(file_path))
    return file_path


async def search_spotify(query: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    data = sp.search(q=query, type="track", limit=10)
    for item in data["tracks"]["items"]:
        results.append(
            {
                "id": item["id"],
                "title": item["name"],
                "artists": ", ".join(a["name"] for a in item["artists"]),
                "url": item["external_urls"]["spotify"],
                "thumb": (item["album"]["images"][0]["url"] if item["album"].get("images") else None),
            }
        )
    return results


async def send_search_results(message: Message, query: str) -> None:
    if message.from_user is None:
        return
    cached = await SEARCH_CACHE.get(f"{message.from_user.id}:{query}")
    if cached:
        results = cached
    else:
        results = await search_spotify(query)
        await SEARCH_CACHE.set(f"{message.from_user.id}:{query}", results)

    keyboard = [
        [
            InlineKeyboardButton(
                f"\U0001F3B5 {item['title']} – {item['artists']}",
                callback_data=item["id"],
            )
        ]
        for item in results
    ]
    await message.reply_text("Choose a track:", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not isinstance(message, Message) or not message.text:
        return
    query = message.text
    await send_search_results(message, query)


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    if not inline_query or not inline_query.query or inline_query.from_user is None:
        return
    query = inline_query.query
    cached = await SEARCH_CACHE.get(f"{inline_query.from_user.id}:{query}")
    if cached:
        results = cached
    else:
        results = await search_spotify(query)
        await SEARCH_CACHE.set(f"{inline_query.from_user.id}:{query}", results)

    articles = []
    for item in results:
        articles.append(
            InlineQueryResultArticle(
                id=item["id"],
                title=f"{item['title']} – {item['artists']}",
                input_message_content=InputTextMessageContent("Downloading..."),
                description="Sending track...",
                thumbnail_url=item.get("thumb"),
            )
        )
    await inline_query.answer(articles)


async def handle_chosen_inline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chosen = update.chosen_inline_result
    if not chosen or chosen.from_user is None:
        return
    await enqueue_download(chosen.from_user.id, chosen.from_user.id, chosen.result_id)


async def enqueue_download(user_id: int, chat_id: int, track_id: str) -> None:
    if APPLICATION is None:
        return
    if queue.full():
        await APPLICATION.bot.send_message(chat_id, "Too many downloads in progress, please try later.")
        return
    await queue.put({"user_id": user_id, "chat_id": chat_id, "track_id": track_id})


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    callback = update.callback_query
    if not callback or not callback.data or not isinstance(callback.message, Message) or callback.from_user is None:
        return
    await callback.answer()
    await enqueue_download(callback.from_user.id, callback.message.chat_id, callback.data)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not isinstance(message, Message) or message.from_user is None:
        return
    if context.args:
        track_id = decode_id(context.args[0])
        await enqueue_download(message.from_user.id, message.chat_id, track_id)
        await message.reply_text("Download queued...")
    else:
        await message.reply_text("Send me a song name or Spotify link.")


async def worker() -> None:
    while True:
        job = await queue.get()
        track_id = job["track_id"]
        chat_id = job["chat_id"]
        if APPLICATION is None:
            queue.task_done()
            continue
        cached = await DOWNLOAD_CACHE.get(track_id)
        if cached and Path(cached).exists():
            file_path = Path(cached)
        else:
            try:
                file_path = await download_track(track_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("download failed", exc_info=exc)
                await APPLICATION.bot.send_message(chat_id, "Download failed.")
                queue.task_done()
                continue
            await DOWNLOAD_CACHE.set(track_id, str(file_path))
        token = encode_id(track_id)
        share_link = f"https://t.me/{APPLICATION.bot.username}?start={token}"
        with file_path.open("rb") as f:
            await APPLICATION.bot.send_audio(
                chat_id,
                audio=InputFile(f, filename=file_path.name),
                caption=f"Share: {share_link}",
            )
        try:
            file_path.unlink()
        except FileNotFoundError:
            pass
        await DOWNLOAD_CACHE.delete(track_id)
        queue.task_done()


def main() -> None:
    global APPLICATION

    async def post_init(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
        app.create_task(run_web())
        for _ in range(QUEUE_MAX_SIZE):
            app.create_task(worker())

    bot_app = (
        Application.builder()
        .token(BOT_TOKEN or "")
        .rate_limiter(AIORateLimiter())
        .post_init(post_init)
        .build()
    )
    APPLICATION = bot_app

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CallbackQueryHandler(button_handler))
    bot_app.add_handler(ChosenInlineResultHandler(handle_chosen_inline))
    bot_app.add_handler(InlineQueryHandler(handle_inline_query))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))

    bot_app.run_polling()


if __name__ == "__main__":
    main()
