# telegrief — per-folder queue and bot-side configuration

Date: 2026-10-16
Status: approved (brainstorm complete), pre-implementation
Supersedes: §4 (configuration), §5.4 (storage), §5.6 (orchestrator), §5.7
(messages), §5.8 (commands) and the scheduling parts of §3 / §5.9 of
`2026-10-15-telegrief-design.md`. Everything not mentioned here (gateways,
collector, scoring strategies, delivery chain, relay, deploy) is unchanged.

## 1. Purpose

v1 builds one digest per daily run; a new run closes the previous digest, so
posts that were never read are lost and reading order is "newest run first".
This change makes reading order purely engagement-driven: every fetch adds
posts to a persistent per-folder queue, and batches always drain the
best-scored posts in the queue, whichever fetch they came from. A user who
has not opened the digest channel for days still sees the best posts first.

At the same time `folders.yaml` goes away: folders and their tuning are stored
in SQLite and edited through the bot, so the server needs no config file.

Success criteria:

- `/folder Memes min_age=24 period=6` makes the service fetch, every 6 hours,
  the posts published between 30 and 24 hours ago from every channel in the
  Telegram folder `Memes`, and add them to the folder's queue.
- The next batch delivered to the digest channel is always the top of the
  queue by score, including posts queued after the current batch was sent.
- No config file, no restart: adding, tuning and removing folders happens
  in the bot chat.

## 2. Decisions (from brainstorming)

| Question | Decision |
|----------|----------|
| Queue retention | Count cap (`max_items`, keep best) **and** post-age cap (`max_age` days). |
| Fetch window when a run is late | Strict: always `[now − (min_age + period), now − min_age)`. Downtime loses posts; no catch-up windows. |
| Schedule anchor | First fetch right after `/folder` creates the folder; then every `period` hours from that moment. No `RUN_AT`. `next=HH:MM` re-anchors. |
| Command shape | One verb: `/folder <name> [key=value ...]`, `/folder <name> off`. |
| Message on fetch | Short note `📥 +N · M queued` in the digest channel. While no batch has gone out since the note, later fetches edit it instead of posting another. |
| Queue storage | `folders` + `queue` tables replace `digests` + `digest_items`. |
| Scheduler | In-process 60 s ticker; apscheduler and PyYAML dropped. |
| Migration | Old digests are dropped, queue starts empty; `targets` (folder name → digest channel) is kept, so a folder re-added under the same exact name reuses its channel. |

Settled in the grilling round:

| Question | Decision |
|----------|----------|
| Window reference time | Scheduled fetches compute the window from the slot time (`next_run_at`), so consecutive slots tile exactly even when the ticker fires late. `/run` uses `now`. |
| Overdue folder (restart after downtime) | Fetch only the latest slot `<= now`; older missed slots are skipped. Downtime is not an expected case. |
| `next_run_at` on failure | Advances anyway; a broken folder is retried next period, not every minute. |
| Fetch note lifecycle | Edited by later fetches until a batch is sent. If the edit fails (note deleted), a new note is posted. |
| Next button | Exactly one, on the last bot message. Notes and footers both carry it; posting either removes it from the previous holder. Folder stores `button_msg_id` + `button_text`. |
| `scoring` / `min_denominator` change | Pending rows are re-scored from stored engagement/views and cached channel subscribers. |
| `/folder X off` | Keeps the digest channel, its `targets` row and its pin in the Telegram folder. |
| Failure notifications | Scheduled fetch failure DMs the owner only when the error text differs from `last_error` (reset on success); `/status` shows `last_error`. Manual `/run` replies in chat, no DM. |
| Digest channel uniqueness | `target=` must not point at a channel already mapped to another folder. |
| Concurrency | Due folders are fetched one at a time. |

## 3. Data model

Migration `002_queue.sql`:

```sql
CREATE TABLE folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,          -- as typed in /folder; matched to Telegram loosely
    min_age_h INTEGER NOT NULL,         -- default 24, >= 0
    period_h INTEGER NOT NULL,          -- default 24, >= 1
    batch_size INTEGER NOT NULL,        -- default 10, >= 1
    scoring TEXT NOT NULL,              -- raw | per_subscriber | per_view, default per_view
    min_denominator INTEGER NOT NULL,   -- default 200, >= 1
    max_items INTEGER NOT NULL,         -- default 1000, >= 1; pending-queue cap
    max_age_d INTEGER NOT NULL,         -- default 7, >= 1; posts older than this are dropped
    next_run_at TEXT NOT NULL,
    last_fetch_at TEXT,
    last_batch_at TEXT,
    last_error TEXT,                    -- last scheduled-fetch error, NULL after a success
    fetch_msg_id INTEGER,               -- live fetch note, NULL once a batch was sent
    fetch_added INTEGER NOT NULL DEFAULT 0,  -- posts added since the last batch
    button_msg_id INTEGER,              -- message currently holding the Next button
    button_text TEXT,                   -- its text, to re-send it without the button
    created_at TEXT NOT NULL
);

CREATE TABLE queue (
    folder_id INTEGER NOT NULL REFERENCES folders (id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,            -- first id of the album
    msg_ids TEXT NOT NULL,              -- JSON list
    date TEXT NOT NULL,                 -- post date (earliest in album)
    engagement INTEGER NOT NULL,
    views INTEGER NOT NULL,
    score REAL NOT NULL,
    noforwards INTEGER NOT NULL DEFAULT 0,
    preview TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,                -- pending | sent | failed
    delivery TEXT,                      -- forward | copy | link
    sent_msg_id INTEGER,                -- last message id delivered in the target
    sent_at TEXT,                       -- batch key: every item of a batch shares it
    queued_at TEXT NOT NULL,
    PRIMARY KEY (folder_id, channel_id, msg_id)
);

CREATE INDEX queue_folder_state_score ON queue (folder_id, state, score);

DROP TABLE digest_items;
DROP TABLE digests;
```

Unchanged tables: `channels`, `posts`, `targets`. `PRAGMA foreign_keys=ON` is
set on connect so the cascade works.

Validation (pydantic `FolderConfig`, applied to every create/update):
`min_age_h >= 0`, `period_h >= 1`, `batch_size >= 1`, `min_denominator >= 1`,
`max_items >= 1`, `max_age_d >= 1`, `scoring` in the strategy set, and
`max_age_d * 24 > min_age_h + period_h` (otherwise every post would be pruned
the moment it is queued). A `target=` channel already mapped to another
folder is rejected.

## 4. Fetch

Runs under a per-folder `asyncio.Lock`, triggered by the ticker (due folder)
or by `/run`.

1. **Window.** Reference time `t` is the slot time `next_run_at` for a
   scheduled fetch and `now` for `/run`. `start = t − (min_age_h + period_h)`,
   `end = t − min_age_h`, half-open `[start, end)`. Slots tile exactly, so a
   late ticker never leaves a gap. `/run <folder> <from_h> <to_h>` overrides
   both bounds (relative to `now`).
2. **Collect and score.** As today: folder → channels (minus digest targets)
   → posts → albums → `rank()` (drop zero engagement, sort). `rank()` no
   longer truncates; the cap is applied to the queue.
3. **Enqueue.** For each scored post, keyed `(folder_id, channel_id, msg_id)`:
   - row exists with state `sent` or `failed` → skip;
   - row exists `pending` → update `engagement`, `views`, `score`, `preview`,
     `noforwards`, `msg_ids`;
   - no row → insert `pending` with `queued_at = now`; counts as *added*.
4. **Prune.** Delete every row whose post `date < now − max_age_d days`,
   regardless of state (sent rows that old can no longer be re-fetched by the
   schedule, so they are not needed for dedupe). Then keep only the top
   `max_items` pending rows ordered by `(score desc, engagement desc,
   date asc)` and delete the rest.
5. **Fetch note.** `fetch_added += added`. Text `📥 +{fetch_added} ·
   {pending} queued`, carrying the `Next {batch_size}` button when
   pending > 0. If `fetch_msg_id` is set, edit that message (text and button);
   if the edit fails, log a warning and fall through to sending a new one.
   Otherwise, if `added > 0`, send the note and store its id. Sending a note
   moves the button: the previous holder (`button_msg_id`) is edited to drop
   it, and the note becomes the holder. Silent, HTML, no preview.
   `last_fetch_at = now`, `last_error = NULL`.
6. **Kick.** Run the read-marker check for this folder (§5): if no batch has
   ever been sent and the queue is not empty, send one; otherwise behave as
   if a read update had arrived (send the next batch when the last one is
   read enough, paced as usual).
7. **Errors.** Collection failure on a scheduled fetch: set `last_error` to
   the error text and DM the owner `❌ <folder>: <error>` only if it differs
   from the previous `last_error`. On `/run` the error is the chat reply and
   no DM is sent. `SessionDead` → owner DM, fatal as today. Channel-level
   errors are skipped and listed in the `/run` reply as today.

Because every post is scored at an age of about `min_age_h`, scores from
different fetches are comparable and a single global order per folder is
meaningful.

### Schedule

A ticker task wakes every 60 s. For every folder with `next_run_at <= now`,
one folder at a time: advance `next_run_at` by `period_h` until only one
more step would put it past `now` (that slot is the latest complete one),
fetch with that slot as reference time, then advance once more into the
future. Older missed slots are skipped, not replayed, and the advance
happens whether or not the fetch succeeded. `/run` never changes
`next_run_at`. A new folder gets `next_run_at = now`, so its first fetch
happens within a minute. On restart the ticker runs immediately, so an
overdue folder fetches at once.

## 5. Batches and read tracking

`send_batch(folder_id, manual)`, under a per-folder lock:

1. Prune (§4.4) so aged-out items are never delivered.
2. Take the top `batch_size` pending items by `(score desc, engagement desc,
   date asc)`.
3. Deliver each exactly as today (forward → copy → link; failures mark the
   item `failed`). Stamp every item of the batch with the same `sent_at =
   now`.
4. Clear the fetch note state: `fetch_msg_id = NULL`, `fetch_added = 0`. The
   note message stays in the channel.
5. Footer: one line per delivered item, `#n · score · engagement/views ·
   channel` where `n` is the position inside the batch, then `queued {pending}`
   or `✅ queue empty`. Button `Next {batch_size}` with callback
   `next:<folder_id>` only when pending > 0. The current button holder
   (previous footer or fetch note) is edited to drop its button; the footer
   becomes the holder. `last_batch_at = now`.

Nothing is sent when the queue is empty.

`on_read(channel_id, max_id)`: look up the folder whose digest channel is
`channel_id`; batch = items with `sent_at = last_batch_at`; apply the
existing 90 % rule and pacing (`AUTO_NEXT_MIN_INTERVAL_S`, single deferred
recheck); send when pending > 0.

`on_next_button(folder_id)`: manual send, no pacing.

Startup catch-up: for every folder with a digest channel mapping and pending
items, read the marker and run `on_read`; a folder with no batch ever sent
and a non-empty queue gets a batch.

## 6. Commands (owner only)

- `/folder <name> [key=value ...]` — create the folder with defaults, or
  update the given keys. Name = the words before the first `key=value`;
  `off` is reserved. Keys:

  | key | meaning |
  |-----|---------|
  | `min_age` | hours, ≥ 0 |
  | `period` | hours, ≥ 1 |
  | `batch` | posts per batch |
  | `scoring` | `raw`, `per_subscriber`, `per_view` |
  | `min_denominator` | floor for views / subscribers |
  | `max_items` | pending-queue cap |
  | `max_age` | days; posts older are dropped |
  | `target` | digest channel id; writes the `targets` row (bot must be admin there) |
  | `next` | `HH:MM` in `TZ` (next occurrence) or `now`; re-anchors the schedule |

  Unknown key or invalid value → error, nothing changed. Changing `period`
  keeps `next_run_at` unless `next=` is given. Changing `scoring` or
  `min_denominator` re-scores every pending row from its stored engagement
  and views plus the cached channel subscribers (a channel missing from the
  cache scores with `min_denominator` as the denominator). `/folder <name>` without
  pairs creates the folder with defaults when it does not exist, and shows
  its config when it does. Reply examples:

  ```
  /folder Memes min_age=24 period=6
  ✅ Memes: every 6h, posts 24–30h old, first fetch within a minute
  /folder Memes batch=20 max_age=7
  ✅ Memes: batch 20, max_age 7d
  ```

- `/folder <name> off` — delete the folder and its queue. The digest channel
  and its `targets` mapping are kept.
- `/folders` — per folder: `Memes · 24/6h · batch 10 · per_view · queued 37 ·
  next 2026-10-16 14:00`, followed by the resolved channel list as today.
  Starts with the Telegram folder titles the account sees.
- `/run [folder] [from_h] [to_h]` — fetch now; the window defaults to the
  folder's own `min_age`/`period`.
- `/status` — per folder: last fetch time and `+added`, pending, last batch
  time, next run, and `⚠️ last_error` when set. Times in `TZ`.
- `/reset <folder>` — unchanged.
- anything else — usage.

Folder names are matched exactly first, then ignoring emoji, case and
spacing (existing `normalize_title`), both for commands and when resolving
the Telegram folder.

## 7. Settings and operations

Removed: `RUN_AT`, `WINDOW_FROM_H`, `WINDOW_TO_H`, `FOLDERS_FILE`,
`config/folders.yaml`, `config/folders.yaml.example`, the compose `config`
mount, the `apscheduler` and `pyyaml` dependencies.

Kept: `TG_API_ID`, `TG_API_HASH`, `BOT_TOKEN`, `TZ` (display and `next=`),
`AUTO_NEXT_MIN_INTERVAL_S`, `AUTO_NEXT_READ_FRACTION`, `MAX_COPY_MB`,
`DATA_DIR`, `LOG_LEVEL`.

CLI: `telegrief run-now [--folder NAME] [--from-h N] [--to-h N]` stays
(fetch, enqueue, kick, exit); `login` and `serve` unchanged.

Server: the repo's `docker-compose.yml` loses the `./config` mount; the
server copy must be updated by hand (no checkout there). After the first
deploy the owner runs `/folder <name> min_age=... period=...` once per
folder. The `/status` and `/folders` commands show the result.

## 8. Code map

| Module | Change |
|--------|--------|
| `models.py` | `FolderConfig` (pydantic, tuning fields, validation), `Folder` (id, name, config, runtime fields), `QueueItem`; drop `Digest`, `DigestItem`, `DigestStatus`. |
| `config.py` | `Settings` without the removed fields; keep `strip_symbols` / `normalize_title`; drop yaml loading and `FoldersConfig`. |
| `storage.py` | folders: `create_folder`, `update_folder`, `delete_folder`, `get_folder` (by id / loose name), `list_folders`, `due_folders(now)`, `folder_for_target`; queue: `enqueue(folder_id, scored, now) -> added`, `prune(folder_id, now, cfg)`, `rescore(folder_id, cfg, channels)`, `pending_items(folder_id, limit)`, `count_pending`, `batch_items(folder_id, sent_at)`, `update_item`. Digest methods removed. |
| `digest.py` | `DigestService.fetch(folder, start, end, scheduled)`, `send_batch(folder_id, manual)`, `on_read`, `on_next_button`, `catch_up`, `kick(folder)`, `_move_button(folder, msg_id, text)`. |
| `scheduler.py` | `Ticker` replaces apscheduler (`latest_slot` helper); `serve` starts it; `run_once` unchanged in shape. |
| `commands.py` | `/folder` parser (`parse_folder_args`), `/folders`, `/status`, `/run`, `/reset`. |
| `delivery.py` | `render_footer(batch, channels, cfg, pending)`, `render_fetch_note(added, pending)`; `render_title` removed. |
| `migrations/002_queue.sql` | as in §3. |
| `tg/bot.py` | callback `next:<folder_id>`; no change in shape. |

## 9. Testing

Unit tests with the existing in-memory fakes and a temporary SQLite file:

- `storage`: migration 002 on a 001 database keeps `targets`; folder CRUD
  and loose-name lookup; `enqueue` inserts, updates pending, skips sent and
  failed, returns `added`; `prune` drops by age (any state) and caps pending
  by score; `due_folders`; `folder_for_target`.
- `models`: `FolderConfig` validation including the `max_age` vs
  `min_age + period` rule.
- `digest`: fetch enqueues and posts a note with the button, previous footer
  loses its button; second fetch with no batch in between edits the note; a
  failed edit falls back to a new note; a batch clears the note state so the
  next fetch posts a new one; fetch with an idle channel sends a batch;
  fetch with an unread batch only enqueues; a later batch delivers the best
  of the whole queue; `on_read` and pacing unchanged; catch-up sends the
  first batch for a never-sent folder; scheduled failure DMs once per
  distinct error and clears on success; manual `/run` failure does not DM.
- `scheduler`: ticker runs due folders one at a time, fetches with the latest
  slot `<= now` as reference time, advances `next_run_at` past `now` even
  on failure, skips folders not yet due, keeps going when one folder fails.
- `commands`: `/folder` parsing (name with spaces, pairs, `off`, reserved
  name, unknown key, bad value, duplicate `target`), reply texts; scoring
  change re-scores pending rows; `/run` default window from the folder;
  `/status` with `last_error`, `/folders` lines.
- `delivery`: footer with and without button, fetch note text.
