from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path
import contextlib
from typing import Any, Dict, List

from aiocache import Cache
from fastapi import FastAPI
import uvicorn
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
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
    MessageHandler,
    filters,
)
from savify import Savify
from savify.types import Format, Quality
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

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

APPLICATION: Application

sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=SPOTIFY_ID, client_secret=SPOTIFY_SECRET))

app = FastAPI()

async def run_web() -> None:
    config = uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


@app.get("/healthz")  # type: ignore[misc]
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


def encode_id(sid: str) -> str:
    return base64.urlsafe_b64encode(sid.encode()).decode().rstrip("=")


def decode_id(token: str) -> str:
    padding = "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(token + padding).decode()


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
            }
        )
    return results


async def send_search_results(message: Message, query: str) -> None:
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
    query = update.message.text
    await send_search_results(update.message, query)


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query.query
    if not query:
        return
    cached = await SEARCH_CACHE.get(f"{update.inline_query.from_user.id}:{query}")
    if cached:
        results = cached
    else:
        results = await search_spotify(query)
        await SEARCH_CACHE.set(f"{update.inline_query.from_user.id}:{query}", results)

    articles = []
    for item in results:
        token = encode_id(item["id"])
        articles.append(
            InlineQueryResultArticle(
                id=item["id"],
                title=f"{item['title']} – {item['artists']}",
                input_message_content=None,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Download \U0001F53D", url=f"https://t.me/{context.bot.username}?start={token}")]]
                ),
                description="Send to download",
            )
        )
    await update.inline_query.answer(articles)


async def enqueue_download(user_id: int, chat_id: int, track_id: str) -> None:
    if queue.full():
        await APPLICATION.bot.send_message(chat_id, "Too many downloads in progress, please try later.")
        return
    await queue.put({"user_id": user_id, "chat_id": chat_id, "track_id": track_id})


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await enqueue_download(query.from_user.id, query.message.chat_id, query.data)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        track_id = decode_id(context.args[0])
        await enqueue_download(update.message.from_user.id, update.message.chat_id, track_id)
        await update.message.reply_text("Download queued...")
    else:
        await update.message.reply_text("Send me a song name or Spotify link.")


async def worker() -> None:
    while True:
        job = await queue.get()
        track_id = job["track_id"]
        chat_id = job["chat_id"]
        cached = await DOWNLOAD_CACHE.get(track_id)
        if cached:
            await APPLICATION.bot.send_audio(chat_id, audio=InputFile(cached))
            queue.task_done()
            continue
        url = f"https://open.spotify.com/track/{track_id}"
        savify = Savify(
            quality=Quality.BEST,
            download_format=Format.MP3,
            path=DOWNLOAD_DIR,
            group="%artist%/%album%",
            spotify_credentials=(SPOTIFY_ID, SPOTIFY_SECRET),
        )
        try:
            await asyncio.to_thread(savify.download, url)
        except Exception as exc:  # noqa: BLE001
            logger.error("download failed", exc_info=exc)
            await APPLICATION.bot.send_message(chat_id, "Download failed.")
            queue.task_done()
            continue
        file_path = next(DOWNLOAD_DIR.rglob(f"{track_id}*.mp3"))
        await DOWNLOAD_CACHE.set(track_id, str(file_path))
        token = encode_id(track_id)
        share_link = f"https://t.me/{APPLICATION.bot.username}?start={token}"
        await APPLICATION.bot.send_audio(
            chat_id,
            audio=InputFile(file_path),
            caption=f"Share: {share_link}",
        )
        queue.task_done()


async def main() -> None:
    global APPLICATION
    bot_app = Application.builder().token(BOT_TOKEN).rate_limiter(AIORateLimiter()).build()
    APPLICATION = bot_app

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CallbackQueryHandler(button_handler))
    bot_app.add_handler(InlineQueryHandler(handle_inline_query))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))

    for _ in range(QUEUE_MAX_SIZE):
        bot_app.create_task(worker())

    await bot_app.initialize()
    await bot_app.start()
    web_task = asyncio.create_task(run_web())
    await bot_app.updater.start_polling()
    await bot_app.updater.idle()
    web_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await web_task


if __name__ == "__main__":
    asyncio.run(main())
