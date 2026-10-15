# telegrief

Daily ranked digest of Telegram channel posts from your folders. Every day at
`RUN_AT` (default 02:00 UTC) it collects posts published in the window
`[now-48h, now-24h)` from every channel inside each configured Telegram folder,
ranks them by engagement (reactions per view by default), and forwards the
best ones into a private digest channel it creates for you. The first batch
arrives immediately; the next batch arrives once you have read most (90%)
of the previous one.

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
cp config/folders.yaml.example config/folders.yaml   # edit: your Telegram folder titles
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
- anything else (`/start`, `/help`) — usage.

## Configuration

Environment (compose `environment:` or `/run/secrets/<name>` files):
`TG_API_ID`, `TG_API_HASH`, `BOT_TOKEN`, `TZ` (UTC), `RUN_AT` (02:00),
`WINDOW_FROM_H` (48), `WINDOW_TO_H` (24), `AUTO_NEXT_MIN_INTERVAL_S` (120),
`AUTO_NEXT_READ_FRACTION` (0.9, share of a batch you must read before the next
one is sent automatically),
`MAX_COPY_MB` (200), `LOG_LEVEL` (INFO).

`config/folders.yaml`: see `config/folders.yaml.example`. Re-read on every run and
bot command, no restart needed. Folder `name`
matches the Telegram folder title ignoring emoji (including animated ones),
case and extra spaces, so `name: mem` finds the folder `😊mEm`; `/folders` prints
the titles the service sees. Bot commands accept the same loose form
(`/run mem`). The auto-created digest channel is titled after the real Telegram
folder title with emoji removed (`mEm · digest`); rename it freely, the
service only remembers its id. The service adds that channel to the source
folder and pins it at the top (once; unpin by hand if you like), and never
reads digest channels as sources even though they sit in the folder.

## Development

```bash
uv sync
./check.sh          # ruff, mypy, pytest
./check.sh lint     # autofix
cp docker-compose.override.yml.example docker-compose.override.yml  # local image
```

## Deploy

Every push to `main` builds the image and then SSHes to the server, passing
the digest of the image it just published; `deploy.sh` pins that digest in
`.env` (`TELEGRIEF_IMAGE=…@sha256:…`), pulls it and restarts the service.
Nothing is ever deployed by tag. Roll back with `./deploy.sh sha256:<digest>`
on the server, or re-run an earlier workflow's `deploy` job. The server has **no
git checkout**: it owns `docker-compose.yml`, `deploy.sh`, `.env`, `config/`,
`secrets/` and `.data/`, so a push to `main` can change only what runs inside
the container, never how it runs on the host. When the repo's reference copies
of `docker-compose.yml` or `deploy.sh` change, update the server by hand.

The container runs as `PUID:PGID` with a read-only root filesystem, all
capabilities dropped and `no-new-privileges`; only `./.data` is writable.
Logs go to journald so they survive the recreate every deploy does:
`docker compose logs app` for the current container, or
`journalctl CONTAINER_NAME=telegrief-app-1 --since -1d` for history.

One-time setup:

1. On the server, in an empty service directory: copy `docker-compose.yml` and
   `deploy.sh` from the repo, create `config/folders.yaml` and `secrets/`, run
   `login` (see Quick start), and point the container at the owner of those
   files:

   ```bash
   printf 'PUID=%s\nPGID=%s\n' "$(id -u)" "$(id -g)" > .env
   ```

2. Generate a key and restrict it to the deploy script on the server:

   ```bash
   ssh-keygen -t ed25519 -N "" -C telegrief-deploy -f deploy_key
   echo 'command="/path/to/telegrief/deploy.sh",restrict '"$(cat deploy_key.pub)" \
     | ssh server 'cat >> ~/.ssh/authorized_keys'
   ```

3. In the GitHub repo, three secrets (secrets are masked in the public job
   log, variables are not): `DEPLOY_SSH_KEY` (contents of `deploy_key`),
   `DEPLOY_TARGET` (`user@host`), `DEPLOY_KNOWN_HOSTS` (`ssh-keyscan -t ed25519
   <host>` output, verified against a trusted `known_hosts`). Delete the local
   `deploy_key` afterwards.
