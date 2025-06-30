# Spotify DL Bot

Telegram bot to search and download Spotify music.

## Quick start

```bash
docker compose up -d
```

Set the following environment variables in `docker-compose.yml`:

| Variable | Description |
| --- | --- |
| `BOT_TOKEN` | Telegram bot token |
| `SPOTIPY_CLIENT_ID` | Spotify client ID |
| `SPOTIPY_CLIENT_SECRET` | Spotify client secret |
| `REDIS_URL` | Redis connection URL (optional) |
