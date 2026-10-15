# telegrief — design spec

Date: 2026-10-15
Status: approved (brainstorm + grilling rounds complete), pre-implementation

## 1. Purpose

Too many posts in Telegram channels to read everything. telegrief runs daily at
02:00 UTC, collects every post published in the window `[now-48h, now-24h)`
from all channels inside chosen Telegram folders, scores them by engagement
normalized for channel size, and forwards the best ones into a private digest
channel. It sends the first batch (top N) immediately and sends the next batch
automatically once the previous one has been read.

Success criteria:

- Every morning each configured folder has a fresh digest in its own private
  channel, best posts first, as native forwards (media visible, "Forwarded
  from" header links back to the original post and its comments).
- Adding or removing a channel in the Telegram folder requires no config
  change; the next run picks it up. Digest channels are created by the service
  itself; config is a list of folder names plus optional tuning.
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
  post. Message ids in a private chat are per-account, so the ids the user
  session gets back are not the bot's ids: the bot learns relay ids from its
  own incoming updates (one item through the relay at a time, with a timeout
  that falls back to a link).
- **Auto-next uses the read marker.** Telegram sends `UpdateReadChannelInbox`
  to the user session when the user reads a channel on any device. When the
  marker passes the last post of the current batch, the next batch is sent,
  paced to at most one automatic batch per `AUTO_NEXT_MIN_INTERVAL_S`.
- **Destination:** one private channel per folder, created by the user session
  on first run (bot invited and promoted to admin). Optional override with an
  existing channel id.
- **Default scoring:** `per_view` = engagement / max(views, floor), where
  engagement is the reaction count, or the forward count for channels that have
  reactions disabled. Strategies are pluggable per folder.
- **Protected content** ("restrict saving content") cannot be forwarded. The
  user session downloads the media (≤ `MAX_COPY_MB`), sends it to the relay,
  and the bot re-sends it by media reference with a caption linking to the
  original. Larger media, or a failed copy, falls back to a link message.
- **Stack:** Python 3.12 on `python:3.12-slim`, Telethon 1.45 + cryptg (both
  user and bot clients), aiosqlite, apscheduler, pydantic-settings, PyYAML,
  loguru, click. Dependencies managed with uv (`pyproject.toml` + `uv.lock`),
  linting with ruff + mypy, tests with pytest.
- **Deploy:** single container, docker compose, public multi-arch image
  (`linux/amd64`, `linux/arm64`) built by GitHub Actions and pushed to
  `ghcr.io/ffoxin/telegrief`; the server pulls. Repository:
  `https://github.com/ffoxin/telegrief`, MIT license.

## 3. Architecture

One long-running process (`telegrief serve`) holding:

- a Telethon **user client** (reads folders, channels, posts, reactions,
  subscribers; creates digest channels; forwards or copies to relay; receives
  read-marker updates),
- a Telethon **bot client** (forwards or re-sends from relay into digest
  channels, posts and edits title/footer messages, handles the manual "Next"
  button and owner commands, reports failures to the owner),
- an **apscheduler** cron job firing at `RUN_AT` in `TZ`,
- a **SQLite** database (WAL) in the data volume.

Data flow for one run, per folder:

```
folder title ──GetDialogFilters──▶ channel list
channel ──GetFullChannel (cached 24h)──▶ subscribers, reactions enabled, noforwards
channel ──iter_messages(window)──▶ raw posts ──group albums──▶ posts
posts ──filter engagement>0, scoring strategy──▶ ranked list (≤ max_items)
ranked list ──▶ digest + digest_items (SQLite)
digest ──send_batch──▶ per item: forward | copy | link via relay ──▶ digest channel
                     ──▶ bot: footer (scores, counter, Next button)
user reads channel ──UpdateReadChannelInbox──▶ on_read ──▶ send_batch (paced)
```

## 4. Configuration

Environment variables (pydantic-settings, `secrets_dir=/run/secrets` so each
value may also come from a file named after the variable in lowercase):

| Variable                   | Default             | Meaning                                        |
|----------------------------|---------------------|------------------------------------------------|
| `TG_API_ID`                | required            | from my.telegram.org                           |
| `TG_API_HASH`              | required            | from my.telegram.org                           |
| `BOT_TOKEN`                | required            | from @BotFather                                |
| `TZ`                       | `UTC`               | timezone for `RUN_AT` and window math          |
| `RUN_AT`                   | `02:00`             | daily run time, `HH:MM`                        |
| `WINDOW_FROM_H`            | `48`                | window start = now − this many hours           |
| `WINDOW_TO_H`              | `24`                | window end = now − this many hours             |
| `AUTO_NEXT_MIN_INTERVAL_S` | `120`               | minimum seconds between automatic batches      |
| `MAX_COPY_MB`              | `200`               | protected media above this size → link fallback|
| `DATA_DIR`                 | `/app/data`         | sessions, sqlite, tmp, heartbeat               |
| `FOLDERS_FILE`             | `/app/folders.yaml` | folder config path                             |
| `LOG_LEVEL`                | `INFO`              | loguru level                                   |

`folders.yaml`:

```yaml
folders:
  - name: Memes            # Telegram folder title, exact match
    batch_size: 10         # default 10
    scoring: per_view      # raw | per_subscriber | per_view (default per_view)
    min_denominator: 200   # floor for views (per_view) or subscribers (per_subscriber); default 200
    max_items: 1000        # digest length cap; default 1000
  - name: News
    scoring: per_subscriber
    min_denominator: 1000
    target: -1001234567890 # optional: use this existing channel (bot must be admin) instead of auto-creating
```

Config is validated at startup; an invalid file aborts `serve` with a clear
error. Folder names must be unique. The file is re-read at the start of every
run so edits apply without restart.

## 5. Components

Package `telegrief` under `src/`, installed as a console script `telegrief`:

| Module            | Responsibility |
|-------------------|----------------|
| `cli.py`          | click commands: `login`, `run-now [--folder NAME] [--from-h N] [--to-h N]`, `serve` (default) |
| `config.py`       | `Settings` (env) and `FoldersConfig` / `FolderConfig` (yaml) models |
| `models.py`       | dataclasses: `ChannelInfo`, `RawPost`, `Post` (album-aggregated), `Digest`, `DigestItem` |
| `scoring.py`      | engagement selection, strategy registry (`raw`, `per_subscriber`, `per_view`), display formatting |
| `storage.py`      | aiosqlite repository; applies `migrations/*.sql` in order |
| `collector.py`    | folder → channels → posts; album grouping; channel info cache |
| `targets.py`      | resolve or create the digest channel for a folder; `/reset` support |
| `delivery.py`     | item delivery (forward / copy / link via relay), title and footer rendering |
| `digest.py`       | orchestrator: `build_digest`, `send_batch`, `on_read` with pacing, `on_next_button` |
| `commands.py`     | owner-only bot commands `/run`, `/status`, `/folders`, `/reset` |
| `scheduler.py`    | apscheduler setup, `serve` loop, startup catch-up, heartbeat |
| `tg/protocols.py` | `UserGateway`, `BotGateway` Protocols (what the rest of the app depends on) |
| `tg/user.py`      | Telethon user client wrapper implementing `UserGateway` |
| `tg/bot.py`       | Telethon bot client wrapper implementing `BotGateway` |

### 5.1 Gateways (Telethon isolation)

```python
class UserGateway(Protocol):
    me_id: int
    async def folder_channels(self, folder_name: str) -> list[int]: ...
    async def channel_info(self, channel_id: int) -> ChannelInfo: ...     # username, title, subscribers, reactions_enabled, noforwards
    def iter_posts(self, channel_id: int, start: datetime, end: datetime) -> AsyncIterator[RawPost]: ...
    async def create_digest_channel(self, title: str, bot_username: str) -> int: ...  # create, invite bot, promote admin
    async def forward_to_relay(self, channel_id: int, msg_ids: list[int]) -> list[int]: ...
    async def copy_to_relay(self, channel_id: int, msg_ids: list[int], caption: str, max_bytes: int) -> list[int]: ...
    async def read_marker(self, channel_id: int) -> int: ...
    def on_channel_read(self, handler: Callable[[int, int], Awaitable[None]]) -> None: ...
    async def ping_bot(self, bot_username: str) -> None: ...   # sends /start so the bot caches the owner peer

class BotGateway(Protocol):
    username: str
    async def forward_from_relay(self, target: int, relay_ids: list[int]) -> list[int]: ...
    async def resend_from_relay(self, target: int, relay_ids: list[int], caption: str) -> list[int]: ...  # by media reference
    async def delete_relay(self, relay_ids: list[int]) -> None: ...
    async def send_text(self, target: int, text: str, next_digest_id: int | None = None) -> int: ...
    async def edit_text(self, target: int, msg_id: int, text: str, next_digest_id: int | None = None) -> None: ...
    async def send_owner(self, text: str) -> None: ...
    def on_next_button(self, handler: Callable[[int], Awaitable[None]]) -> None: ...
    def on_owner_command(self, handler: Callable[[str, list[str]], Awaitable[str]]) -> None: ...
```

All digest-channel messages are sent with `silent=True`. Everything outside
`tg/` depends only on these Protocols, so the orchestrator, scoring, storage,
targets and commands are unit-tested with in-memory fakes.

### 5.2 Collector

- **Folder resolution.** `messages.GetDialogFiltersRequest` → list of
  `DialogFilter` / `DialogFilterChatlist` (skip `DialogFilterDefault`). Title
  may be `str` or `TextWithEntities` (newer layers); compare the plain text,
  exact match, case-sensitive. Channels = `include_peers` ∪ `pinned_peers`;
  if the filter has the `broadcasts` flag, add every broadcast channel from the
  dialog list; remove `exclude_peers`. Keep only entities that are `Channel`
  with `broadcast=True` and `megagroup=False`. Folder not found → error for
  that folder (reported to owner), other folders continue.
- **Channel info.** `channels.GetFullChannelRequest` → `participants_count`,
  `available_reactions` (`ChatReactionsNone` → `reactions_enabled=False`),
  channel `noforwards`. Stored in `channels` table with `updated_at`; reused
  if younger than 24h (the call is flood-limited).
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
def engagement(post, channel):          return post.reactions if channel.reactions_enabled else post.forwards
def raw(e, post, channel, cfg):            return float(e)
def per_subscriber(e, post, channel, cfg): return e / max(channel.subscribers, cfg.min_denominator) * 1000
def per_view(e, post, channel, cfg):       return e / max(post.views, cfg.min_denominator)
```

Posts with `engagement == 0` are dropped before ranking. All reactions count
equally (no sentiment weighting). Ranking: score descending, tie-break
engagement descending, then date ascending; truncated to `max_items`.
Display format per strategy: `raw` → `120`, `per_subscriber` → `3.4/1k`,
`per_view` → `4.1%`. Engagement is shown as `120👍` or, for forward-based
channels, `35↗️`.

### 5.4 Storage

SQLite, WAL mode, file `DATA_DIR/telegrief.db`. Migrations are plain SQL files
`migrations/NNN_name.sql` applied in order; `schema_version` tracks the last
applied number.

```sql
channels(id INTEGER PK, username TEXT, title TEXT, subscribers INTEGER,
         reactions_enabled INTEGER, noforwards INTEGER, updated_at TEXT)
targets(folder TEXT PK, channel_id INTEGER, created_at TEXT)
posts(channel_id INTEGER, msg_id INTEGER, grouped_ids TEXT, date TEXT, views INTEGER,
      forwards INTEGER, reactions INTEGER, replies INTEGER, preview TEXT,
      noforwards INTEGER, fetched_at TEXT, PRIMARY KEY(channel_id, msg_id))
digests(id INTEGER PK AUTOINCREMENT, folder TEXT, target INTEGER, window_start TEXT,
        window_end TEXT, total INTEGER, title_msg_id INTEGER, footer_msg_id INTEGER,
        status TEXT,            -- open|done|failed
        last_batch_at TEXT, created_at TEXT)
digest_items(digest_id INTEGER, rank INTEGER, channel_id INTEGER, msg_id INTEGER,
             msg_ids TEXT, engagement INTEGER, views INTEGER, score REAL,
             state TEXT,        -- pending|sent|failed
             delivery TEXT,     -- forward|copy|link
             sent_msg_id INTEGER, sent_at TEXT, PRIMARY KEY(digest_id, rank))
```

`posts` is append/upsert history (later analytics, dedupe). `digest_items.msg_id`
is the first id of the album; `msg_ids` is the JSON list used for forwarding;
`sent_msg_id` is the **last** message id delivered for the item (albums span
several ids), which is what the read marker is compared against.

### 5.5 Targets (digest channels)

- `resolve_target(folder_cfg)`: if `folder_cfg.target` is set, use it. Else
  look up `targets` by folder name. Else `UserGateway.create_digest_channel`
  with title `"{folder} · digest"`, which creates a private broadcast channel,
  invites the bot and promotes it with post/edit/delete rights; store the
  mapping.
- If the target later becomes unusable (channel deleted, bot removed, no
  rights) the digest is marked `failed` and the owner is notified. No automatic
  re-creation. `/reset <folder>` deletes the mapping row; the next run creates
  a fresh channel.

### 5.6 Digest orchestrator

- **`build_digest(folder_cfg, now, from_h, to_h)`**: compute window, resolve
  target, collect, filter, score, rank, insert `digests` row (`status=open`,
  `total=len`) and `digest_items` (`state=pending`). Mark any other `open`
  digest for the same `target` as `done` so stale digests can never fire. Post
  the title message. Zero items → title says "no posts in window", status
  `done`. Else `send_batch(digest, manual=True)`.
- **`send_batch(digest, manual)`**: under a per-digest `asyncio.Lock`. Take
  the next `batch_size` pending items in rank order. For each item:
  1. not `noforwards` → user `forward_to_relay`, bot `forward_from_relay`,
     bot `delete_relay`; `delivery=forward`;
  2. `noforwards` (or `ChatForwardsRestrictedError` from step 1) and media
     size ≤ `MAX_COPY_MB` → user `copy_to_relay` (download to `DATA_DIR/tmp`,
     send as new message, delete temp file), bot `resend_from_relay` with
     caption `↗️ @channel · <link>` + original text (truncated to the caption
     limit), bot `delete_relay`; `delivery=copy`;
  3. too large, or copy failed → bot `send_text` with
     `https://t.me/<username>/<id>` or `https://t.me/c/<bare_id>/<id>`;
     `delivery=link`;
  4. any other exception → item `state=failed`, log, continue.
  Albums are forwarded / copied as one album. After the loop: remove the
  button from the previous footer (edit), send the new footer, set
  `last_batch_at`. If no pending items remain → footer says complete, no
  button, `status=done`.
- **`on_read(channel_id, max_id)`** (from `UserGateway.on_channel_read`):
  find the `open` digest whose `target` matches; ignore if none, if
  `max_id <` the highest `sent_msg_id`, or if no pending items remain. If
  `now - last_batch_at ≥ AUTO_NEXT_MIN_INTERVAL_S` → `send_batch(manual=False)`.
  Otherwise schedule a single deferred recheck at
  `last_batch_at + interval` (one per digest; later events while one is
  scheduled are ignored) which calls `UserGateway.read_marker(target)` and
  applies the same rule. Events are never dropped into a stuck state.
- **`on_next_button(digest_id)`** (from `BotGateway.on_next_button`):
  `send_batch(manual=True)` if digest is `open`; pacing does not apply.
- **Startup catch-up** (`serve`): for every `open` digest call
  `UserGateway.read_marker(target)` and run the `on_read` logic, so a batch
  read while the service was down still triggers the next one.
- **Failures.** Collection or delivery failure for a folder → digest
  `status=failed`, owner notified via `send_owner`, other folders continue.
  `AuthKeyUnregisteredError` or similar session death on the user client →
  owner notified if the bot still works, log at CRITICAL, exit with code 1
  (operator must re-run `login`).

### 5.7 Messages

Title (one per digest, posted before batch 1, never edited):

```
📂 Memes · 2026-10-14 · 57 posts from 23 channels
```

Footer (one per batch, HTML, link previews off, inline button `Next N` with
callback data `next:<digest_id>`; the previous footer is edited to drop its
button when a new one is sent):

```
#1 · 4.1% · 120👍 / 2.9k👁 · @channel_a
#2 · 3.8% · 45👍 / 1.2k👁 · Channel B
...
shown 10/57
```

Last footer: `✅ all 57 shown`. Empty digest: title says `no posts in window`,
no footer.

Relay chat hygiene: `ping_bot` sends `/start` at `serve` startup; the bot
deletes it. Relay copies are deleted immediately after re-forwarding or
re-sending.

### 5.8 Owner commands (bot DM, owner = `UserGateway.me_id`, others ignored)

- `/run [folder] [from_h] [to_h]` — run now for one folder or all; hours
  default to `WINDOW_FROM_H` / `WINDOW_TO_H` (e.g. `/run Memes 24 0` = last
  24h). Replies with a one-line result per folder.
- `/status` — per folder: open digest date, shown/total, pending count, or
  "none".
- `/folders` — per folder: resolved channel list (title, @username,
  subscribers) without running; verifies folder detection.
- `/reset <folder>` — drop the target mapping (see §5.5).

### 5.9 CLI

- `telegrief login` — interactive Telethon login for the user session (phone,
  code, 2FA). Writes `DATA_DIR/user.session`. Run once via
  `docker compose run --rm -it app telegrief login`.
- `telegrief run-now [--folder NAME] [--from-h N] [--to-h N]` — one-off run
  and exit. Must not be used while `serve` is running (two processes on one
  session file); prefer `/run` in the bot chat.
- `telegrief serve` — connect, apply migrations, `ping_bot`, startup catch-up,
  schedule daily run, touch `DATA_DIR/heartbeat` every 60s, listen for read
  updates, button presses and commands forever. Default container command.

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
  check.sh                  (uv run ruff format / ruff check / mypy / pytest)
  update.sh
  pyproject.toml            (metadata, [project.scripts], ruff + mypy + pytest config)
  uv.lock
  LICENSE                   (MIT)
  README.md
```

## 7. Docker, deploy, CI

- **Image:** `python:3.12-slim`; uv binary copied from the pinned
  `ghcr.io/astral-sh/uv` image; `uv sync --frozen --no-dev` into
  `/app/.venv`; `src/` and `migrations/` copied; `CMD ["telegrief", "serve"]`.
- **Compose:** one service `app`; `image: ghcr.io/ffoxin/telegrief:latest`;
  `restart: unless-stopped`; env `TZ=UTC`, `LOG_LEVEL`; secrets `tg_api_id`,
  `tg_api_hash`, `bot_token` from `./secrets/*.txt` mounted at `/run/secrets`;
  volumes `./.data:/app/data` and `./folders.yaml:/app/folders.yaml:ro`;
  healthcheck fails when `/app/data/heartbeat` is older than 5 minutes;
  json-file logging capped like the other bots. `update.sh` (author's gist)
  pulls and restarts.
- **First run on server:** fill `folders.yaml` and `secrets/`,
  `docker compose run --rm -it app telegrief login`, `docker compose up -d`,
  then `/folders` and `/run` in the bot chat to verify.
- **CI (`ci.yml`):**
  - job `check` on every push and PR: `astral-sh/setup-uv`, `uv sync
    --frozen`, `uv run ruff format --check`, `uv run ruff check`,
    `uv run mypy src`, `uv run pytest`.
  - job `image` on push to `main` and on tags, needs `check`: QEMU + buildx,
    `docker/login-action` to GHCR with `GITHUB_TOKEN`
    (`permissions: packages: write`), `docker/metadata-action` tags
    `latest`, `sha-<short>`, and the git tag when present,
    `docker/build-push-action` for `linux/amd64,linux/arm64`. Package is
    public, so the server needs no registry login.

## 8. Testing

Unit tests (pytest, pytest-asyncio), no live Telegram:

- `scoring`: engagement selection (reactions vs forwards), each strategy,
  floor behaviour, zero-engagement filtering, `max_items` truncation,
  ordering and tie-breaks, display formatting.
- `collector`: album grouping (sum/max/earliest), window boundary (stop at
  `date < start`, exclude `date ≥ end`), service messages skipped, folder
  resolution with fake filters (include/pinned/exclude/broadcasts flag,
  non-broadcast entities dropped, missing folder error), channel info cache
  expiry.
- `storage`: migrations apply once, digest/item/target round-trips, "mark
  older open digests done", on a temporary database.
- `targets`: yaml override wins, existing mapping reused, creation stores
  mapping, reset clears it.
- `digest`: batch sends N items and records last ids; `on_read` below marker
  → nothing; at/above marker → next batch; pacing defers instead of dropping;
  stale digest ignored; manual button bypasses pacing; last batch marks done;
  forward → copy → link fallback chain; oversize media → link; item failure
  does not stop batch; previous footer loses its button.
- `commands`: owner-only filter, `/run` argument parsing and defaults,
  `/status` and `/folders` rendering, `/reset`.
- `config`: env + secrets_dir, folders.yaml defaults, duplicate names and
  invalid values rejected.

Manual smoke test: `/folders` then `/run Memes 24 0` in the bot chat before
trusting the first scheduled run.

## 9. Out of scope (v1)

- Cross-channel duplicate detection (same meme reposted by several channels).
- Sentiment-weighted reactions, comments in the score.
- Deleting old digest messages (retention).
- Megagroups / discussion groups inside folders.
- Web UI, multi-user support, per-user accounts.
- Catching up missed days (a run that did not happen is not replayed).
