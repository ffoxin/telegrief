"""SQLite persistence (aiosqlite, WAL) with plain-SQL migrations."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from .config import normalize_title
from .models import (
    ChannelInfo,
    Delivery,
    Folder,
    FolderConfig,
    ItemState,
    QueueItem,
    RawPost,
    ScoredPost,
)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
PENDING_ORDER = "ORDER BY score DESC, engagement DESC, date ASC"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _req_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _channel_from_row(row: aiosqlite.Row) -> ChannelInfo:
    return ChannelInfo(
        id=row["id"],
        username=row["username"],
        title=row["title"],
        subscribers=row["subscribers"],
        reactions_enabled=bool(row["reactions_enabled"]),
        noforwards=bool(row["noforwards"]),
    )


def _folder_from_row(row: aiosqlite.Row) -> Folder:
    return Folder(
        id=row["id"],
        name=row["name"],
        config=FolderConfig(
            min_age_h=row["min_age_h"],
            period_h=row["period_h"],
            batch_size=row["batch_size"],
            scoring=row["scoring"],
            min_denominator=row["min_denominator"],
            max_items=row["max_items"],
            max_age_d=row["max_age_d"],
        ),
        next_run_at=_req_dt(row["next_run_at"]),
        created_at=_req_dt(row["created_at"]),
        last_fetch_at=_dt(row["last_fetch_at"]),
        last_batch_at=_dt(row["last_batch_at"]),
        last_error=row["last_error"],
        fetch_msg_id=row["fetch_msg_id"],
        fetch_added=row["fetch_added"],
        button_msg_id=row["button_msg_id"],
        button_text=row["button_text"],
    )


def _queue_item_from_row(row: aiosqlite.Row) -> QueueItem:
    return QueueItem(
        folder_id=row["folder_id"],
        channel_id=row["channel_id"],
        msg_ids=tuple(json.loads(row["msg_ids"])),
        date=_req_dt(row["date"]),
        engagement=row["engagement"],
        views=row["views"],
        score=row["score"],
        noforwards=bool(row["noforwards"]),
        preview=row["preview"],
        queued_at=_req_dt(row["queued_at"]),
        state=ItemState(row["state"]),
        delivery=Delivery(row["delivery"]) if row["delivery"] else None,
        sent_msg_id=row["sent_msg_id"],
        sent_at=_dt(row["sent_at"]),
    )


class Storage:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    async def open(cls, path: Path | str) -> Storage:
        conn = await aiosqlite.connect(str(path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        storage = cls(conn)
        await storage.migrate()
        return storage

    async def close(self) -> None:
        await self._conn.close()

    async def migrate(self) -> int:
        await self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        row = await self._fetchone("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version")
        current = int(row["v"]) if row is not None else 0
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            number = int(path.name.split("_", 1)[0])
            if number <= current:
                continue
            await self._conn.executescript(path.read_text(encoding="utf-8"))
            await self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (number,))
            await self._conn.commit()
            current = number
        return current

    async def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        cur = await self._conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        cur = await self._conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)

    # ---- channels ----------------------------------------------------------

    async def upsert_channel(self, info: ChannelInfo, now: datetime) -> None:
        await self._conn.execute(
            """
            INSERT INTO channels
                (id, username, title, subscribers, reactions_enabled, noforwards, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username = excluded.username,
                title = excluded.title,
                subscribers = excluded.subscribers,
                reactions_enabled = excluded.reactions_enabled,
                noforwards = excluded.noforwards,
                updated_at = excluded.updated_at
            """,
            (
                info.id,
                info.username,
                info.title,
                info.subscribers,
                int(info.reactions_enabled),
                int(info.noforwards),
                now.isoformat(),
            ),
        )
        await self._conn.commit()

    async def get_channel(self, channel_id: int) -> tuple[ChannelInfo, datetime] | None:
        row = await self._fetchone("SELECT * FROM channels WHERE id = ?", (channel_id,))
        if row is None:
            return None
        return _channel_from_row(row), _req_dt(row["updated_at"])

    # ---- posts ---------------------------------------------------------------

    async def upsert_posts(self, posts: Iterable[RawPost], now: datetime) -> None:
        await self._conn.executemany(
            """
            INSERT INTO posts
                (channel_id, msg_id, grouped_id, date, views, forwards, reactions, replies,
                 preview, noforwards, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, msg_id) DO UPDATE SET
                views = excluded.views,
                forwards = excluded.forwards,
                reactions = excluded.reactions,
                replies = excluded.replies,
                preview = excluded.preview,
                noforwards = excluded.noforwards,
                fetched_at = excluded.fetched_at
            """,
            [
                (
                    p.channel_id,
                    p.msg_id,
                    p.grouped_id,
                    p.date.isoformat(),
                    p.views,
                    p.forwards,
                    p.reactions,
                    p.replies,
                    p.preview,
                    int(p.noforwards),
                    now.isoformat(),
                )
                for p in posts
            ],
        )
        await self._conn.commit()

    async def count_posts(self, channel_id: int) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM posts WHERE channel_id = ?", (channel_id,)
        )
        return int(row["n"]) if row is not None else 0

    # ---- targets -------------------------------------------------------------

    async def get_target(self, folder: str) -> int | None:
        row = await self._fetchone("SELECT channel_id FROM targets WHERE folder = ?", (folder,))
        return int(row["channel_id"]) if row is not None else None

    async def set_target(self, folder: str, channel_id: int, now: datetime) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO targets (folder, channel_id, created_at) VALUES (?, ?, ?)",
            (folder, channel_id, now.isoformat()),
        )
        await self._conn.commit()

    async def all_target_ids(self) -> list[int]:
        rows = await self._fetchall("SELECT channel_id FROM targets")
        return [int(r["channel_id"]) for r in rows]

    async def target_owner(self, channel_id: int) -> str | None:
        """Name under which ``channel_id`` is mapped, whether or not that folder still exists."""
        row = await self._fetchone(
            "SELECT folder FROM targets WHERE channel_id = ? ORDER BY folder LIMIT 1", (channel_id,)
        )
        return str(row["folder"]) if row is not None else None

    async def delete_target(self, folder: str) -> bool:
        cur = await self._conn.execute("DELETE FROM targets WHERE folder = ?", (folder,))
        await self._conn.commit()
        return cur.rowcount > 0

    # ---- folders -------------------------------------------------------------

    async def create_folder(self, name: str, cfg: FolderConfig, now: datetime) -> Folder:
        cur = await self._conn.execute(
            """
            INSERT INTO folders
                (name, min_age_h, period_h, batch_size, scoring, min_denominator, max_items,
                 max_age_d, next_run_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                cfg.min_age_h,
                cfg.period_h,
                cfg.batch_size,
                cfg.scoring,
                cfg.min_denominator,
                cfg.max_items,
                cfg.max_age_d,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        await self._conn.commit()
        folder_id = cur.lastrowid
        assert folder_id is not None
        return Folder(id=folder_id, name=name, config=cfg, next_run_at=now, created_at=now)

    async def update_folder_config(self, folder_id: int, cfg: FolderConfig) -> None:
        await self._conn.execute(
            """
            UPDATE folders SET min_age_h = ?, period_h = ?, batch_size = ?, scoring = ?,
                min_denominator = ?, max_items = ?, max_age_d = ?
            WHERE id = ?
            """,
            (
                cfg.min_age_h,
                cfg.period_h,
                cfg.batch_size,
                cfg.scoring,
                cfg.min_denominator,
                cfg.max_items,
                cfg.max_age_d,
                folder_id,
            ),
        )
        await self._conn.commit()

    async def update_folder_state(self, folder: Folder) -> None:
        """Persist the runtime fields only; config and schedule have their own writers."""
        await self._conn.execute(
            """
            UPDATE folders SET last_fetch_at = ?, last_batch_at = ?, last_error = ?,
                fetch_msg_id = ?, fetch_added = ?, button_msg_id = ?, button_text = ?
            WHERE id = ?
            """,
            (
                _iso(folder.last_fetch_at),
                _iso(folder.last_batch_at),
                folder.last_error,
                folder.fetch_msg_id,
                folder.fetch_added,
                folder.button_msg_id,
                folder.button_text,
                folder.id,
            ),
        )
        await self._conn.commit()

    async def set_next_run(self, folder_id: int, when: datetime) -> None:
        await self._conn.execute(
            "UPDATE folders SET next_run_at = ? WHERE id = ?", (when.isoformat(), folder_id)
        )
        await self._conn.commit()

    async def delete_folder(self, folder_id: int) -> None:
        await self._conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        await self._conn.commit()

    async def get_folder(self, folder_id: int) -> Folder | None:
        row = await self._fetchone("SELECT * FROM folders WHERE id = ?", (folder_id,))
        return _folder_from_row(row) if row is not None else None

    async def find_folder(self, name: str) -> Folder | None:
        """Folder by name: exact match first, then ignoring emoji, case and spacing."""
        folders = await self.list_folders()
        exact = next((f for f in folders if f.name == name), None)
        if exact is not None:
            return exact
        wanted = normalize_title(name)
        return next((f for f in folders if normalize_title(f.name) == wanted), None)

    async def list_folders(self) -> list[Folder]:
        rows = await self._fetchall("SELECT * FROM folders ORDER BY id")
        return [_folder_from_row(r) for r in rows]

    async def due_folders(self, now: datetime) -> list[Folder]:
        rows = await self._fetchall(
            "SELECT * FROM folders WHERE next_run_at <= ? ORDER BY next_run_at, id",
            (now.isoformat(),),
        )
        return [_folder_from_row(r) for r in rows]

    async def folder_for_target(self, channel_id: int) -> Folder | None:
        row = await self._fetchone(
            """
            SELECT f.* FROM folders f JOIN targets t ON t.folder = f.name
            WHERE t.channel_id = ? ORDER BY f.id LIMIT 1
            """,
            (channel_id,),
        )
        return _folder_from_row(row) if row is not None else None

    # ---- queue ---------------------------------------------------------------

    async def _count_rows(self, folder_id: int) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM queue WHERE folder_id = ?", (folder_id,)
        )
        return int(row["n"]) if row is not None else 0

    async def enqueue(self, folder_id: int, scored: Iterable[ScoredPost], now: datetime) -> int:
        """Insert new posts as pending, refresh pending ones, leave sent/failed alone.

        Returns the number of newly inserted rows.
        """
        before = await self._count_rows(folder_id)
        await self._conn.executemany(
            """
            INSERT INTO queue
                (folder_id, channel_id, msg_id, msg_ids, date, engagement, views, score,
                 noforwards, preview, state, queued_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            ON CONFLICT(folder_id, channel_id, msg_id) DO UPDATE SET
                msg_ids = excluded.msg_ids,
                engagement = excluded.engagement,
                views = excluded.views,
                score = excluded.score,
                noforwards = excluded.noforwards,
                preview = excluded.preview
            WHERE queue.state = 'pending'
            """,
            [
                (
                    folder_id,
                    s.post.channel_id,
                    s.post.msg_id,
                    json.dumps(list(s.post.msg_ids)),
                    s.post.date.isoformat(),
                    s.engagement,
                    s.post.views,
                    s.score,
                    int(s.post.noforwards),
                    s.post.preview,
                    now.isoformat(),
                )
                for s in scored
            ],
        )
        await self._conn.commit()
        return await self._count_rows(folder_id) - before

    async def prune(self, folder_id: int, cfg: FolderConfig, now: datetime) -> None:
        """Drop posts older than max_age (any state), then cap pending rows to max_items."""
        cutoff = (now - timedelta(days=cfg.max_age_d)).isoformat()
        await self._conn.execute(
            "DELETE FROM queue WHERE folder_id = ? AND date < ?", (folder_id, cutoff)
        )
        await self._conn.execute(
            f"""
            DELETE FROM queue WHERE folder_id = ? AND state = 'pending' AND rowid NOT IN (
                SELECT rowid FROM queue WHERE folder_id = ? AND state = 'pending'
                {PENDING_ORDER} LIMIT ?
            )
            """,
            (folder_id, folder_id, cfg.max_items),
        )
        await self._conn.commit()

    async def pending_items(self, folder_id: int, limit: int | None = None) -> list[QueueItem]:
        rows = await self._fetchall(
            f"""
            SELECT * FROM queue WHERE folder_id = ? AND state = 'pending'
            {PENDING_ORDER} LIMIT ?
            """,
            (folder_id, -1 if limit is None else limit),
        )
        return [_queue_item_from_row(r) for r in rows]

    async def count_items(self, folder_id: int, state: ItemState) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM queue WHERE folder_id = ? AND state = ?", (folder_id, state)
        )
        return int(row["n"]) if row is not None else 0

    async def count_pending(self, folder_id: int) -> int:
        return await self.count_items(folder_id, ItemState.PENDING)

    async def batch_items(self, folder_id: int, sent_at: datetime) -> list[QueueItem]:
        """Items sent in one batch: send_batch stamps them all with the batch time."""
        rows = await self._fetchall(
            f"""
            SELECT * FROM queue WHERE folder_id = ? AND state = 'sent' AND sent_at = ?
            {PENDING_ORDER}
            """,
            (folder_id, sent_at.isoformat()),
        )
        return [_queue_item_from_row(r) for r in rows]

    async def update_item(self, item: QueueItem) -> None:
        await self._conn.execute(
            """
            UPDATE queue SET state = ?, delivery = ?, sent_msg_id = ?, sent_at = ?
            WHERE folder_id = ? AND channel_id = ? AND msg_id = ?
            """,
            (
                item.state,
                item.delivery,
                item.sent_msg_id,
                _iso(item.sent_at),
                item.folder_id,
                item.channel_id,
                item.msg_id,
            ),
        )
        await self._conn.commit()

    async def update_scores(self, folder_id: int, scores: Iterable[tuple[int, int, float]]) -> None:
        """Re-score pending rows: (channel_id, msg_id, score) triples."""
        await self._conn.executemany(
            """
            UPDATE queue SET score = ?
            WHERE folder_id = ? AND channel_id = ? AND msg_id = ? AND state = 'pending'
            """,
            [(score, folder_id, channel_id, msg_id) for channel_id, msg_id, score in scores],
        )
        await self._conn.commit()
