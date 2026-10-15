# telegrief

Ranked queue of Telegram channel posts from your folders. For every folder
you register with the bot, the service fetches, every `period` hours, the
posts published between `min_age` and `min_age + period` hours ago from every
channel in that Telegram folder, scores them by engagement (reactions per
view by default) and adds them to the folder's queue. Batches are delivered
into a private digest channel the service creates for you, best post first,
whichever fetch it came from; the next batch arrives once you have read most
(90%) of the previous one, or when you press **Next**. Posts wait in the
queue up to `max_age` days, so a few days away never cost you the best ones.

Design: `docs/superpowers/specs/2026-10-15-telegrief-design.md` and
`docs/superpowers/specs/2026-10-16-queue-design.md`.

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
docker compose pull
docker compose run --rm -it app telegrief login   # phone, code, 2FA once
docker compose up -d
```

Then open the bot chat and register a folder:

```
/folder Memes min_age=24 period=6
```

The first fetch happens within a minute; `/folders` shows the channels the
service resolved, `/status` the queue.

## Bot commands (owner only)

- `/folder <name> [key=value ...]` — add a folder (defaults apply) or change
  settings. Keys: `min_age` (hours, default 24), `period` (hours, default 24),
  `batch` (posts per batch, 10), `scoring` (`per_view` | `per_subscriber` |
  `raw`), `min_denominator` (floor for views / subscribers, 200), `max_items`
  (queue cap, 1000), `max_age` (days a post may wait, 7), `target` (use an
  existing channel id where the bot is admin), `next` (`HH:MM` in `TZ`, or
  `now`: when the next fetch runs). `/folder <name>` alone shows the settings.
- `/folder <name> off` — remove the folder and its queue; the digest channel
  stays.
- `/folders` — every folder with its settings, queue size, next fetch and the
  channels resolved from the Telegram folder.
- `/run [folder] [from_h] [to_h]` — fetch now; the window defaults to the
  folder's `min_age` / `period`, e.g. `/run Memes 24 0` for the last 24 hours.
- `/status` — last fetch, queue size, last batch, next fetch and the last
  error per folder.
- `/reset <folder>` — forget the digest channel and take it out of the folder;
  the next fetch creates a new one. The old channel itself is kept.
- anything else (`/start`, `/help`) — usage.

Folder names are matched ignoring emoji, case and extra spaces, so
`/folder mem period=6` finds the Telegram folder `😊mEm` and the digest
channel is titled after the real title with emoji removed (`mEm · digest`).
The service pins that channel at the top of the source folder (once; unpin
by hand if you like) and never reads digest channels as sources.

## How a fetch works

Every minute the service checks which folders are due. A due folder fetches
the window anchored to its scheduled slot, so a late run never leaves a gap;
if the service was down for several slots only the latest one is fetched.
New posts go into the queue (re-fetched ones are refreshed, already shown
ones are skipped), posts older than `max_age` are dropped, and the queue is
capped to the best `max_items`. The digest channel gets a short note
`📥 +N · M queued` (edited in place by later fetches until a batch goes out)
and, if the last batch is already read, the next batch right away.

A scheduled fetch that fails sends you one message per distinct error;
`/status` shows the last error until a fetch succeeds.

## Configuration

Environment (compose `environment:` or `/run/secrets/<name>` files):
`TG_API_ID`, `TG_API_HASH`, `BOT_TOKEN`, `TZ` (UTC; used for `next=` and for
times in replies), `AUTO_NEXT_MIN_INTERVAL_S` (120), `AUTO_NEXT_READ_FRACTION`
(0.9, share of a batch you must read before the next one is sent
automatically), `MAX_COPY_MB` (200), `DATA_DIR` (`/app/data`), `LOG_LEVEL`
(INFO). Everything per folder is set with `/folder` and stored in the SQLite
database under `DATA_DIR`.

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
Nothing is ever deployed by tag.

Upgrading from the `folders.yaml` era: remove the `./config` mount and the
`RUN_AT` variable from the server's `docker-compose.yml`, deploy, then
register each folder once with `/folder <name> min_age=… period=…` (the
existing digest channels are reused when the name matches exactly). Roll back with `./deploy.sh sha256:<digest>`
on the server, or re-run an earlier workflow's `deploy` job. The server has **no
git checkout**: it owns `docker-compose.yml`, `deploy.sh`, `.env`,
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
   `deploy.sh` from the repo, create `secrets/`, run
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
