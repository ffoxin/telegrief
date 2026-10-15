"""SQLite persistence (aiosqlite, WAL) with plain-SQL migrations."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .models import (
    ChannelInfo,
    Delivery,
    Digest,
    DigestItem,
    DigestStatus,
    ItemState,
    RawPost,
    ScoredPost,
)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


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


def _digest_from_row(row: aiosqlite.Row) -> Digest:
    return Digest(
        id=row["id"],
        folder=row["folder"],
        target=row["target"],
        window_start=_req_dt(row["window_start"]),
        window_end=_req_dt(row["window_end"]),
        total=row["total"],
        status=DigestStatus(row["status"]),
        created_at=_req_dt(row["created_at"]),
        title_msg_id=row["title_msg_id"],
        footer_msg_id=row["footer_msg_id"],
        footer_text=row["footer_text"],
        last_batch_at=_dt(row["last_batch_at"]),
    )


def _item_from_row(row: aiosqlite.Row) -> DigestItem:
    return DigestItem(
        digest_id=row["digest_id"],
        rank=row["rank"],
        channel_id=row["channel_id"],
        msg_ids=tuple(json.loads(row["msg_ids"])),
        engagement=row["engagement"],
        views=row["views"],
        score=row["score"],
        noforwards=bool(row["noforwards"]),
        preview=row["preview"],
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

    async def delete_target(self, folder: str) -> bool:
        cur = await self._conn.execute("DELETE FROM targets WHERE folder = ?", (folder,))
        await self._conn.commit()
        return cur.rowcount > 0

    # ---- digests -------------------------------------------------------------

    async def create_digest(
        self,
        folder: str,
        target: int,
        window_start: datetime,
        window_end: datetime,
        scored: list[ScoredPost],
        now: datetime,
    ) -> Digest:
        await self._conn.execute(
            "UPDATE digests SET status = ? WHERE target = ? AND status = ?",
            (DigestStatus.DONE, target, DigestStatus.OPEN),
        )
        cur = await self._conn.execute(
            """
            INSERT INTO digests
                (folder, target, window_start, window_end, total, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                folder,
                target,
                window_start.isoformat(),
                window_end.isoformat(),
                len(scored),
                DigestStatus.OPEN,
                now.isoformat(),
            ),
        )
        digest_id = cur.lastrowid
        assert digest_id is not None
        await self._conn.executemany(
            """
            INSERT INTO digest_items
                (digest_id, rank, channel_id, msg_id, msg_ids, engagement, views, score,
                 noforwards, preview, state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    digest_id,
                    rank,
                    s.post.channel_id,
                    s.post.msg_id,
                    json.dumps(list(s.post.msg_ids)),
                    s.engagement,
                    s.post.views,
                    s.score,
                    int(s.post.noforwards),
                    s.post.preview,
                    ItemState.PENDING,
                )
                for rank, s in enumerate(scored, start=1)
            ],
        )
        await self._conn.commit()
        return Digest(
            id=digest_id,
            folder=folder,
            target=target,
            window_start=window_start,
            window_end=window_end,
            total=len(scored),
            status=DigestStatus.OPEN,
            created_at=now,
        )

    async def get_digest(self, digest_id: int) -> Digest | None:
        row = await self._fetchone("SELECT * FROM digests WHERE id = ?", (digest_id,))
        return _digest_from_row(row) if row is not None else None

    async def open_digest_for_target(self, target: int) -> Digest | None:
        row = await self._fetchone(
            "SELECT * FROM digests WHERE target = ? AND status = ? ORDER BY id DESC LIMIT 1",
            (target, DigestStatus.OPEN),
        )
        return _digest_from_row(row) if row is not None else None

    async def open_digests(self) -> list[Digest]:
        rows = await self._fetchall(
            "SELECT * FROM digests WHERE status = ? ORDER BY id", (DigestStatus.OPEN,)
        )
        return [_digest_from_row(r) for r in rows]

    async def latest_digest(self, folder: str) -> Digest | None:
        row = await self._fetchone(
            "SELECT * FROM digests WHERE folder = ? ORDER BY id DESC LIMIT 1", (folder,)
        )
        return _digest_from_row(row) if row is not None else None

    async def update_digest(self, digest: Digest) -> None:
        """Persist title/footer/status/last_batch_at. Only an OPEN row is updated, so a
        batch that was in flight when a new run closed the digest cannot reopen it."""
        await self._conn.execute(
            """
            UPDATE digests SET title_msg_id = ?, footer_msg_id = ?, footer_text = ?,
                status = ?, last_batch_at = ?
            WHERE id = ? AND status = ?
            """,
            (
                digest.title_msg_id,
                digest.footer_msg_id,
                digest.footer_text,
                digest.status,
                _iso(digest.last_batch_at),
                digest.id,
                DigestStatus.OPEN,
            ),
        )
        await self._conn.commit()

    # ---- items ---------------------------------------------------------------

    async def pending_items(self, digest_id: int, limit: int) -> list[DigestItem]:
        rows = await self._fetchall(
            """
            SELECT * FROM digest_items WHERE digest_id = ? AND state = ?
            ORDER BY rank LIMIT ?
            """,
            (digest_id, ItemState.PENDING, limit),
        )
        return [_item_from_row(r) for r in rows]

    async def count_items(self, digest_id: int, state: ItemState) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM digest_items WHERE digest_id = ? AND state = ?",
            (digest_id, state),
        )
        return int(row["n"]) if row is not None else 0

    async def batch_items(self, digest_id: int, sent_at: datetime) -> list[DigestItem]:
        """Items sent in one batch: send_batch stamps them all with the batch time."""
        rows = await self._fetchall(
            """
            SELECT * FROM digest_items WHERE digest_id = ? AND state = ? AND sent_at = ?
            ORDER BY rank
            """,
            (digest_id, ItemState.SENT, _iso(sent_at)),
        )
        return [_item_from_row(r) for r in rows]

    async def update_item(self, item: DigestItem) -> None:
        await self._conn.execute(
            """
            UPDATE digest_items SET state = ?, delivery = ?, sent_msg_id = ?, sent_at = ?
            WHERE digest_id = ? AND rank = ?
            """,
            (
                item.state,
                item.delivery,
                item.sent_msg_id,
                _iso(item.sent_at),
                item.digest_id,
                item.rank,
            ),
        )
        await self._conn.commit()
