# telegrief — design spec

Date: 2026-10-15
Status: approved design, pre-implementation

## 1. Purpose

Too many posts in Telegram channels to read everything. telegrief runs daily at
03:00, collects every post published in the window `[now-48h, now-24h)` from all
channels inside chosen Telegram folders, scores them by engagement normalized
for channel size, and forwards the best ones into a private digest channel.
It sends the first batch (top N) immediately and sends the next batch
automatically once the previous one has been read.

Success criteria:

- Every morning each configured folder has a fresh digest in its own private
  channel, best posts first, as native forwards (media visible, "Forwarded
  from" header links back to the original post and its comments).
- Adding or removing a channel in the Telegram folder requires no config
  change; the next run picks it up.
- Reading the whole batch triggers the next batch without pressing anything.
  Not reading it means nothing more arrives.
- Works for several folders (e.g. Memes, News) with independent settings.

## 2. Constraints and key decisions

- **User account (MTProto) is mandatory.** The Bot API cannot list folders,
  read arbitrary channels, or see reaction counts. A user session (Telethon)
  does all reading. One-time interactive login (phone, code, 2FA) creates a
  session file stored in the data volume.
- **A bot does delivery.** If the user account itself posted into the digest
  channel, those posts would be the user's own outgoing messages and would never
  be unread, which breaks read tracking. So: user client forwards posts to the
  bot's private chat (relay), bot re-forwards them into the digest channel and
  deletes the relay copy. Telegram preserves the original "Forwarded from"
  header on a forward of a forward, so the header still links to the original
  post.
- **Auto-next uses the read marker.** Telegram sends `UpdateReadChannelInbox`
  to the user session when the user reads a channel on any device. When the
  marker passes the last post of the current batch, the next batch is sent.
- **Destination:** one private channel per folder. The bot is admin there.
- **Default scoring:** `per_view` = reactions / max(views, floor). Strategies
  are pluggable per folder.
- **Stack:** Python 3.12, Telethon (both user and bot clients), aiosqlite,
  apscheduler, pydantic-settings, loguru, click. Pinned `requirements.txt`
  and `check.sh`, matching the author's other bots.
- **Deploy:** single container, docker compose, image built by GitHub Actions
  and pushed to GHCR; the server pulls.

## 3. Architecture

One long-running process (`telegrief serve`) holding:

- a Telethon **user client** (reads folders, channels, posts, reactions,
  subscribers; forwards to relay; receives read-marker updates),
- a Telethon **bot client** (forwards from relay into digest channels, posts
  and edits header/footer messages, handles the manual "Next" button),
- an **apscheduler** cron job firing at `RUN_AT` in `TZ`,
- a **SQLite** database (WAL) in the data volume.

Data flow for one run, per folder:

```
folder title ──GetDialogFilters──▶ channel list
channel ──GetFullChannel (cached 24h)──▶ subscribers
channel ──iter_messages(window)──▶ raw posts ──group albums──▶ posts
posts ──scoring strategy──▶ ranked list ──▶ digest + digest_items (SQLite)
digest ──send_batch──▶ user: forward to relay ──▶ bot: forward to target, delete relay
                     ──▶ bot: header (create/edit) + batch footer
user reads target ──UpdateReadChannelInbox──▶ on_read ──▶ send_batch (if marker ≥ last sent)
```

## 4. Configuration

Environment variables (pydantic-settings, `secrets_dir=/run/secrets` so each
value may also come from a file named after the variable in lowercase):

| Variable        | Default      | Meaning                                   |
|-----------------|--------------|-------------------------------------------|
| `TG_API_ID`     | required     | from my.telegram.org                      |
| `TG_API_HASH`   | required     | from my.telegram.org                      |
| `BOT_TOKEN`     | required     | from @BotFather                           |
| `TZ`            | `UTC`        | timezone for `RUN_AT` and window math     |
| `RUN_AT`        | `03:00`      | daily run time, `HH:MM`                   |
| `WINDOW_FROM_H` | `48`         | window start = now − this many hours      |
| `WINDOW_TO_H`   | `24`         | window end = now − this many hours        |
| `DATA_DIR`      | `/app/data`  | sessions + sqlite                         |
| `FOLDERS_FILE`  | `/app/folders.yaml` | folder config path                 |
| `LOG_LEVEL`     | `INFO`       | loguru level                              |

`folders.yaml`:

```yaml
folders:
  - name: Memes            # Telegram folder title, exact match
    target: -1001234567890 # private digest channel; bot must be admin
    batch_size: 10
    scoring: per_view      # raw | per_subscriber | per_view
    min_denominator: 200   # floor for views (per_view) or subscribers (per_subscriber)
  - name: News
    target: -1009876543210
    batch_size: 15
    scoring: per_subscriber
    min_denominator: 1000
```

Defaults when omitted: `batch_size: 10`, `scoring: per_view`,
`min_denominator: 200`. Config is validated at startup; an invalid file aborts
`serve` with a clear error. The file is re-read at the start of every run so
edits apply without restart.

## 5. Components

Package `telegrief` under `src/`:

| Module          | Responsibility |
|-----------------|----------------|
| `cli.py`        | click commands: `login`, `run-now [--folder NAME]`, `serve` (default) |
| `config.py`     | `Settings` (env) and `FoldersConfig` / `FolderConfig` (yaml) models |
| `models.py`     | dataclasses: `ChannelInfo`, `RawPost`, `Post` (album-aggregated), `Digest`, `DigestItem` |
| `scoring.py`    | strategy registry: `raw`, `per_subscriber`, `per_view`; `score(post, channel, cfg) -> float` |
| `storage.py`    | aiosqlite repository; applies `migrations/*.sql` in order |
| `collector.py`  | folder → channels → posts; album grouping; subscriber cache |
| `delivery.py`   | relay forward, link fallback, header/footer rendering and posting |
| `digest.py`     | orchestrator: `build_digest`, `send_batch`, `on_read`, `on_next_button` |
| `scheduler.py`  | apscheduler setup, `serve` loop, startup catch-up |
| `tg/user.py`    | Telethon user client wrapper implementing `UserGateway` |
| `tg/bot.py`     | Telethon bot client wrapper implementing `BotGateway` |
| `tg/protocols.py` | `UserGateway`, `BotGateway` Protocols (what the rest of the app depends on) |

### 5.1 Gateways (Telethon isolation)

```python
class UserGateway(Protocol):
    async def folder_channels(self, folder_name: str) -> list[ChannelInfo]: ...
    async def channel_subscribers(self, channel_id: int) -> int: ...
    def iter_posts(self, channel_id: int, start: datetime, end: datetime) -> AsyncIterator[RawPost]: ...
    async def forward_to_relay(self, channel_id: int, msg_ids: list[int]) -> list[int]: ...
    async def read_marker(self, channel_id: int) -> int: ...
    def on_channel_read(self, handler: Callable[[int, int], Awaitable[None]]) -> None: ...
    async def ping_bot(self) -> None: ...   # sends /start to the bot so the bot caches the owner peer

class BotGateway(Protocol):
    async def forward_from_relay(self, target: int, relay_ids: list[int]) -> list[int]: ...
    async def delete_relay(self, relay_ids: list[int]) -> None: ...
    async def send_text(self, target: int, text: str, next_digest_id: int | None = None) -> int: ...
    async def edit_text(self, target: int, msg_id: int, text: str, next_digest_id: int | None = None) -> None: ...
    def on_next_button(self, handler: Callable[[int], Awaitable[None]]) -> None: ...
```

Everything outside `tg/` depends only on these Protocols, so the orchestrator,
scoring and storage are unit-tested with in-memory fakes.

### 5.2 Collector

- **Folder resolution.** `messages.GetDialogFiltersRequest` → list of
  `DialogFilter` / `DialogFilterChatlist` (skip `DialogFilterDefault`). Title
  may be `str` or `TextWithEntities` (newer layers); compare the plain text,
  exact match, case-sensitive. Channels = `include_peers` ∪ `pinned_peers`;
  if the filter has the `broadcasts` flag, add every broadcast channel from the
  dialog list; remove `exclude_peers`. Keep only entities that are `Channel`
  with `broadcast=True` and `megagroup=False`. Folder not found → error for
  that folder, other folders continue.
- **Subscribers.** `channels.GetFullChannelRequest` → `participants_count`.
  Stored in `channels` table with `subs_updated_at`; reused if younger than
  24h (the call is flood-limited).
- **Posts.** `iter_messages(channel, offset_date=window_end)` yields messages
  older than `window_end`, newest first; stop at the first message with
  `date < window_start`. Skip `MessageService`. Capture `id`, `date`,
  `grouped_id`, `views`, `forwards`, `reactions` (sum of
  `reactions.results[].count`, 0 if none), `replies.replies`, first 80 chars
  of text, `noforwards` (message flag OR channel flag).
- **Albums.** Raw posts sharing `grouped_id` become one `Post` with
  `msg_ids` = all ids (ascending), `reactions` = sum, `views` = max,
  `forwards` = max, `date` = earliest.
- **Errors.** `FloodWaitError` ≤ 120s: Telethon sleeps and retries
  (`flood_sleep_threshold=120`). Longer: abort that channel, log, continue.
  `ChannelPrivateError`, `ChannelInvalidError`, left channel: log and skip.

### 5.3 Scoring

```python
def raw(post, channel, cfg):            return float(post.reactions)
def per_subscriber(post, channel, cfg): return post.reactions / max(channel.subscribers, cfg.min_denominator) * 1000
def per_view(post, channel, cfg):       return post.reactions / max(post.views, cfg.min_denominator)
```

All reactions count equally (no sentiment weighting). Ranking: score
descending, tie-break raw reactions descending, then date ascending.
Display format per strategy: `raw` → `120`, `per_subscriber` → `3.4/1k`,
`per_view` → `4.1%`.

### 5.4 Storage

SQLite, WAL mode, file `DATA_DIR/telegrief.db`. Migrations are plain SQL files
`migrations/NNN_name.sql` applied in order; `schema_version` tracks the last
applied number.

```sql
channels(id INTEGER PK, username TEXT, title TEXT, subscribers INTEGER, subs_updated_at TEXT)
posts(channel_id INTEGER, msg_id INTEGER, grouped_ids TEXT, date TEXT, views INTEGER,
      forwards INTEGER, reactions INTEGER, replies INTEGER, preview TEXT,
      noforwards INTEGER, fetched_at TEXT, PRIMARY KEY(channel_id, msg_id))
digests(id INTEGER PK AUTOINCREMENT, folder TEXT, target INTEGER, window_start TEXT,
        window_end TEXT, total INTEGER, header_msg_id INTEGER, status TEXT,  -- open|done|failed
        last_batch_at TEXT, created_at TEXT)
digest_items(digest_id INTEGER, rank INTEGER, channel_id INTEGER, msg_id INTEGER,
             msg_ids TEXT, score REAL, state TEXT,  -- pending|sent|failed
             sent_msg_id INTEGER, sent_at TEXT, PRIMARY KEY(digest_id, rank))
```

`posts` is append/upsert history (later analytics, dedupe). `digest_items.msg_id`
is the first id of the album; `msg_ids` is the JSON list used for forwarding.

### 5.5 Digest orchestrator

- **`build_digest(folder_cfg, now)`**: compute window, collect, score, rank,
  insert `digests` row (`status=open`, `total=len`) and `digest_items`
  (`state=pending`). Mark any other `open` digest for the same `target` as
  `done` so stale digests can never fire. Zero posts → post a header
  "no posts in window", status `done`. Then `send_batch(digest, manual=True)`.
- **`send_batch(digest, manual)`**: under a per-digest `asyncio.Lock`. Take
  the next `batch_size` pending items in rank order. For each item:
  1. if `noforwards` → bot sends a link message (`https://t.me/<username>/<id>`
     or `https://t.me/c/<bare_id>/<id>`), record its id as `sent_msg_id`;
  2. else user client `forward_to_relay`, bot `forward_from_relay`,
     bot `delete_relay`; record the id of the last forwarded message (albums span several ids);
  3. `ChatForwardsRestrictedError` on step 2 → fall back to step 1;
  4. any other exception → item `state=failed`, log, continue.
  After the loop: bot sends the batch footer (ranked lines for this batch);
  create or edit the header; set `last_batch_at`. If no pending items remain
  → header shows complete, `status=done`.
- **`on_read(channel_id, max_id)`** (from `UserGateway.on_channel_read`):
  find the `open` digest whose `target` matches; if `max_id ≥` the highest
  `sent_msg_id` among sent items, pending items remain, and at least 60s passed
  since `last_batch_at` → `send_batch(manual=False)`. Otherwise ignore.
- **`on_next_button(digest_id)`** (from `BotGateway.on_next_button`):
  `send_batch(manual=True)` if digest is `open`; the 60s debounce does not
  apply.
- **Startup catch-up** (`serve`): for every `open` digest call
  `UserGateway.read_marker(target)` and run the `on_read` logic, so a batch
  read while the service was down still triggers the next one.
- **Failures.** Bot not admin / target unreachable → digest `status=failed`,
  logged, other folders continue. `AuthKeyUnregisteredError` or similar
  session death on the user client → log at CRITICAL and exit with code 1
  (operator must re-run `login`).

### 5.6 Messages

Header (one per digest, created before batch 1, edited after every batch,
inline button `Next N` with callback data `next:<digest_id>`):

```
📂 Memes · 2026-10-14
57 posts from 23 channels · shown 10/57
```

Completed digests show `✅ all 57 shown`; empty ones `no posts in window`.

Batch footer (one text message after each batch, HTML, link previews off):

```
#1 · 4.1% · 120👍 / 2.9k👁 · @channel_a
#2 · 3.8% · 45👍 / 1.2k👁 · Channel B
...
```

Relay chat hygiene: `ping_bot` sends `/start` at `serve` startup; the bot
deletes it. Relay copies are deleted immediately after re-forwarding.

### 5.7 CLI

- `telegrief login` — interactive Telethon login for the user session (phone,
  code, 2FA). Writes `DATA_DIR/user.session`. Run once via
  `docker compose run --rm -it app login`.
- `telegrief run-now [--folder NAME]` — connect both clients, run
  `build_digest` for all (or one) folders, send batch 1, exit. Does not listen
  for reads.
- `telegrief serve` — connect, apply migrations, startup catch-up, schedule
  daily run, listen for read updates and button presses forever. Default
  container command.

## 6. Repository layout

```
telegrief/
  .github/workflows/ci.yml
  docs/superpowers/specs/
  migrations/001_init.sql
  src/telegrief/            (modules listed in §5)
  tests/
  folders.yaml.example
  docker-compose.yml
  docker-compose.override.yml.example   (adds build: . for local images)
  Dockerfile
  check.sh
  update.sh
  pyproject.toml            (metadata, black/isort/ruff/mypy/pylint config)
  requirements.txt          (pinned runtime)
  requirements-dev.txt      (-r requirements.txt + lint/test tools)
  README.md
```

## 7. Docker, deploy, CI

- **Image:** `python:3.12-alpine`, venv at `/opt/venv`, `src/` copied to
  `/app`, `CMD ["telegrief", "serve"]`.
- **Compose:** one service `app`; `image: ghcr.io/ffoxin/telegrief:latest`;
  `restart: unless-stopped`; env `TZ`, `LOG_LEVEL`; secrets `tg_api_id`,
  `tg_api_hash`, `bot_token` from `./secrets/*.txt`; volumes
  `./.data:/app/data` and `./folders.yaml:/app/folders.yaml:ro`; json-file
  logging capped like the other bots. `update.sh` (author's gist) pulls and
  restarts.
- **First run on server:** create digest channels, add the bot as admin, fill
  `folders.yaml` and `secrets/`, `docker compose run --rm -it app login`,
  `docker compose up -d`, optionally `docker compose exec app telegrief run-now`.
- **CI (`ci.yml`):**
  - job `check` on every push and PR: Python 3.12, install
    `requirements-dev.txt`, run `ruff check`, `black --check`,
    `isort --check`, `mypy src/`, `pytest`.
  - job `image` on push to `main` and on tags, needs `check`:
    `docker/login-action` to GHCR with `GITHUB_TOKEN`
    (`permissions: packages: write`), `docker/metadata-action` tags
    `latest`, `sha-<short>`, and the git tag when present,
    `docker/build-push-action` for `linux/amd64`.
  - If the package stays private the server needs a one-time
    `docker login ghcr.io` with a PAT that has `read:packages`.

## 8. Testing

Unit tests (pytest, pytest-asyncio), no live Telegram:

- `scoring`: each strategy, floor behaviour, ordering and tie-breaks, display
  formatting.
- `collector`: album grouping (sum/max/earliest), window boundary (stop at
  `date < start`, exclude `date ≥ end`), service messages skipped, folder
  resolution with fake filters (include/pinned/exclude/broadcasts flag,
  non-broadcast entities dropped, missing folder error).
- `storage`: migrations apply once, digest/item round-trips, "mark older open
  digests done", subscriber cache expiry, on a temporary database.
- `digest`: batch sends N items and records ids; `on_read` below marker →
  nothing; at/above marker → next batch; debounce 60s; stale digest ignored;
  manual button bypasses debounce; last batch marks done; noforwards and
  `ChatForwardsRestrictedError` fall back to link; item failure does not stop
  batch.
- `config`: env + secrets_dir, folders.yaml defaults and validation errors.

Manual smoke test: `telegrief run-now --folder Memes` against the real account
before first `serve`.

## 9. Out of scope (v1)

- Cross-channel duplicate detection (same meme reposted by several channels).
- Sentiment-weighted reactions, forwards/comments in the score.
- Web UI, multi-user support, per-user accounts.
- Catching up missed days (a run that did not happen is not replayed).
