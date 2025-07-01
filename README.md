# Spotify DL Bot

Telegram bot to search and download Spotify music.
Requires **Python 3.6+** and [aiogram](https://github.com/aiogram/aiogram).

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
BOT_TOKEN=... SPOTIPY_CLIENT_ID=... SPOTIPY_CLIENT_SECRET=... python bot.py
```

### Environment variables

| Variable | Description |
| --- | --- |
| `BOT_TOKEN` | Telegram bot token |
| `SPOTIPY_CLIENT_ID` | Spotify client ID |
| `SPOTIPY_CLIENT_SECRET` | Spotify client secret |
| `REDIS_URL` | Redis connection URL (optional) |
