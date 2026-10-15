# telegrief

Daily ranked digest of Telegram channel posts from your folders. Every day at
`RUN_AT` (default 02:00 UTC) it collects posts published in the window
`[now-48h, now-24h)` from every channel inside each configured Telegram folder,
ranks them by engagement (reactions per view by default), and forwards the
best ones into a private digest channel it creates for you. The first batch
arrives immediately; the next batch arrives once you have read the previous
one.

Design: `docs/superpowers/specs/2026-10-15-telegrief-design.md`.

## Requirements

- A Telegram **user account** (the service reads folders and channels as you;
  the Bot API cannot). Get `api_id` / `api_hash` at https://my.telegram.org.
- A bot token from @BotFather (the bot posts into digest channels and talks
  to you).
- Docker with compose.

## Setup

```bash
git clone git@github.com:ffoxin/telegrief.git && cd telegrief
mkdir -p secrets .data
echo 123456 > secrets/tg_api_id.txt
echo your_api_hash > secrets/tg_api_hash.txt
echo 123456:bot_token > secrets/bot_token.txt
cp folders.yaml.example folders.yaml   # edit: folder titles exactly as in Telegram
docker compose pull
docker compose run --rm -it app telegrief login   # phone, code, 2FA once
docker compose up -d
```

Then open the bot chat and send `/folders` to verify channel detection, and
`/run Memes 24 0` for a test digest of the last 24 hours.

## Bot commands (owner only)

- `/run [folder] [from_h] [to_h]` — run now; defaults `48 24`.
- `/status` — latest digest per folder.
- `/folders` — channels resolved per folder.
- `/reset <folder>` — forget the digest channel; next run creates a new one.

## Configuration

Environment (compose `environment:` or `/run/secrets/<name>` files):
`TG_API_ID`, `TG_API_HASH`, `BOT_TOKEN`, `TZ` (UTC), `RUN_AT` (02:00),
`WINDOW_FROM_H` (48), `WINDOW_TO_H` (24), `AUTO_NEXT_MIN_INTERVAL_S` (120),
`MAX_COPY_MB` (200), `LOG_LEVEL` (INFO).

`folders.yaml`: see `folders.yaml.example`. Re-read on every run.

## Development

```bash
uv sync
./check.sh          # ruff, mypy, pytest
./check.sh lint     # autofix
cp docker-compose.override.yml.example docker-compose.override.yml  # local image
```

## Update on the server

```bash
./update.sh
```
