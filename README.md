# telegrief

Ranked queue of Telegram channel posts from your folders. For every folder
you register with the bot, the service fetches, every `period` hours, the
posts published between `min_age` and `min_age + period` hours ago from every
channel in that Telegram folder, scores them by engagement (reactions per
view by default) and adds them to the folder's queue. Batches are delivered
into a private digest channel the service creates for you, best post first,
whichever fetch it came from; the next batch arrives once you have read most
(90%) of the previous one, or when you press **Next**. Posts wait in the
queue up to `max_age` days (90), so time away never costs you the best ones.
Every delivered post is indexed, pictures and speech included, so `/find`
gets it back later by a few words.

Design: `docs/superpowers/specs/2026-10-15-telegrief-design.md` and
`docs/superpowers/specs/2026-10-16-queue-design.md`.

## Requirements

- A Telegram **user account** (the service reads folders and channels as you;
  the Bot API cannot). Get `api_id` / `api_hash` at https://my.telegram.org.
- A bot token from @BotFather (the bot posts into digest channels and talks
  to you).
- Optionally an Anthropic API key and a Groq API key, to make pictures and
  speech searchable (see [Search](#search)).
- Docker with compose.

## Setup

```bash
git clone git@github.com:ffoxin/telegrief.git && cd telegrief
mkdir -p secrets .data
echo 123456 > secrets/tg_api_id.txt
echo your_api_hash > secrets/tg_api_hash.txt
echo 123456:bot_token > secrets/bot_token.txt
echo sk-ant-... > secrets/anthropic_api_key.txt   # or `touch` it: pictures not indexed
echo gsk_... > secrets/groq_api_key.txt           # or `touch` it: speech not indexed
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
  (queue cap, 1000), `max_age` (days an undelivered post may wait, 90), `target` (use an
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
- `/find <words>` — delivered posts containing every word, newest first (10
  at most), each with an excerpt and links to the original post and to its
  copy in the digest channel. `/find` alone shows how many posts are indexed.
- `/reindex` — index the delivered posts that are not in the search index yet
  and retry the failed ones.
- `/skip [phrase]` — list the skip phrases, or add one (spaces allowed). A
  post whose text contains a skip phrase anywhere, in any case, is never
  queued; applies to fetches from then on. `/unskip <phrase>` removes one.
- `/reset <folder>` — forget the digest channel and take it out of the folder;
  the next fetch creates a new one. The old channel itself is kept.
- `/help [command]` — the command list, or details and examples of one
  command. The same list fills the client's `/` menu, and `/start` or an
  unknown command points to it.

Folder names are matched ignoring emoji, case and extra spaces, so
`/folder mem period=6` finds the Telegram folder `😊mEm` and the digest
channel is titled after the real title with emoji removed (`mEm · digest`).
The service pins that channel at the top of the source folder (once; unpin
by hand if you like) and never reads digest channels as sources.

## How a fetch works

Every minute the service checks which folders are due. A due folder fetches
the window anchored to its scheduled slot, so a late run never leaves a gap;
if the service was down for several slots only the latest one is fetched.
Posts containing a skip phrase are dropped first. New posts go into the
queue (re-fetched ones are refreshed, already shown ones are skipped),
undelivered posts older than `max_age` are dropped, and the queue is capped
to the best `max_items`. Delivered posts are kept for good. The digest channel gets a short note
`📥 +N · M queued` (edited in place by later fetches until a batch goes out)
and, if the last batch is already read, the next batch right away.

A scheduled fetch that fails sends you one message per distinct error;
`/status` shows the last error until a fetch succeeds.

## Search

A post is indexed in the background right after it is delivered; nothing is
ever posted to the digest channel for it. The index holds the post's text
and two optional parts:

- **Pictures**, when `ANTHROPIC_API_KEY` is set: what Claude reads off them,
  the text on them and a sentence or two about what they show. Every picture
  of an album is read.
- **Speech**, when `GROQ_API_KEY` is set: what Groq's Whisper hears in videos,
  round videos, voice messages and audio files. ffmpeg cuts out the first
  2 minutes of sound; files over 50 MB, GIFs and muted videos are skipped.
  The picture of a video is not looked at.

Entries are kept apart from the queue, so a post stays findable after its
folder is removed.

A word matches anywhere inside the indexed text, ignoring case and ё/е, so
`/find кот` also finds `котом`. A post that cannot be read is retried every
10 minutes, four times; the last try leaves pictures and sound out so the
text is still indexed. A rate limit from Anthropic or Groq is not counted as
a failure: indexing pauses and resumes within 10 minutes. Posts delivered before the index existed are added by
`/reindex`.

## Metrics

With `METRICS_FILE` set, the service writes its metrics every 30 seconds as
a Prometheus text file (`telegrief_*`), meant for node-exporter's textfile
collector: mount that collector's directory into the container and point
`METRICS_FILE` at a `.prom` file in it (see the commented lines in
`docker-compose.yml`). Event counters live in the database, so they survive
restarts:

- per folder: fetches and what they collected (`added` / `known` /
  `skipped`), batches, delivered posts by kind, posts your read marker
  passed, the queue by state, last and next fetch;
- search index: entries by state, indexing attempts, picture and speech
  calls, rate-limit pauses.

`grafana/dashboards/telegrief.json` is the dashboard (generated by
`grafana/dashboards.py`; it queries the datasource with uid `infra-metrics`).

## Configuration

Environment (compose `environment:` or `/run/secrets/<name>` files):
`TG_API_ID`, `TG_API_HASH`, `BOT_TOKEN`, `TZ` (UTC; used for `next=` and for
times in replies), `AUTO_NEXT_MIN_INTERVAL_S` (120), `AUTO_NEXT_READ_FRACTION`
(0.9, share of a batch you must read before the next one is sent
automatically), `MAX_COPY_MB` (200), `ANTHROPIC_API_KEY`
(unset or empty: pictures are not indexed), `INDEX_MODEL` (`claude-haiku-5-5`),
`GROQ_API_KEY` (unset or empty: speech is not indexed), `STT_MODEL`
(`whisper-large-v3`), `METRICS_FILE` (unset: no metrics),
`DATA_DIR` (`/app/data`), `LOG_LEVEL`
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

Upgrading from the `folders.yaml` era: copy `.data/telegrief.db` aside first
(migration 002 drops the old digest tables, so rolling the image back across
this release means restoring that copy), remove the `./config` mount and the
`RUN_AT` variable from the server's `docker-compose.yml`, deploy, then
register each folder once with `/folder <name> min_age=… period=…` (the
existing digest channels are reused when the name matches exactly).

Roll back with `./deploy.sh sha256:<digest>` on the server, or re-run an
earlier workflow's `deploy` job. The server has **no git checkout**: it owns
`docker-compose.yml`, `deploy.sh`, `.env`, `secrets/` and `.data/`, so a push
to `main` can change only what runs inside the container, never how it runs
on the host. When the repo's reference copies of `docker-compose.yml` or
`deploy.sh` change, update the server by hand.

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
