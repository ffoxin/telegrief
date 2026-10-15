from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from telegrief.models import (
    ChannelInfo,
    Delivery,
    FolderConfig,
    ItemState,
    Post,
    RawPost,
    ScoredPost,
)
from telegrief.storage import MIGRATIONS_DIR, Storage

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
CH = ChannelInfo(
    id=-1001, username="a", title="A", subscribers=10, reactions_enabled=True, noforwards=False
)


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = await Storage.open(tmp_path / "t.db")
    yield s
    await s.close()


def scored(
    msg_id: int,
    score: float,
    *,
    channel: ChannelInfo = CH,
    engagement: int = 5,
    date: datetime = NOW - timedelta(hours=30),
) -> ScoredPost:
    post = Post(
        channel_id=channel.id,
        msg_ids=(msg_id, msg_id + 1),
        date=date,
        views=100,
        forwards=0,
        reactions=engagement,
        replies=0,
        preview="hi",
        noforwards=False,
    )
    return ScoredPost(post, channel, engagement, score)


async def test_migrate_is_idempotent(storage: Storage) -> None:
    assert await storage.migrate() == 2
    assert await storage.migrate() == 2


async def test_migration_002_keeps_targets(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = await aiosqlite.connect(str(path))
    await conn.executescript((MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8"))
    await conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    await conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    await conn.execute(
        "INSERT INTO targets (folder, channel_id, created_at) VALUES ('Memes', -100500, ?)",
        (NOW.isoformat(),),
    )
    await conn.commit()
    await conn.close()
    storage = await Storage.open(path)
    try:
        assert await storage.get_target("Memes") == -100500
        assert await storage.list_folders() == []
    finally:
        await storage.close()


async def test_channel_roundtrip(storage: Storage) -> None:
    assert await storage.get_channel(CH.id) is None
    await storage.upsert_channel(CH, NOW)
    got = await storage.get_channel(CH.id)
    assert got == (CH, NOW)
    newer = ChannelInfo(
        id=CH.id,
        username=None,
        title="A2",
        subscribers=11,
        reactions_enabled=False,
        noforwards=True,
    )
    await storage.upsert_channel(newer, NOW + timedelta(hours=1))
    assert await storage.get_channel(CH.id) == (newer, NOW + timedelta(hours=1))


async def test_posts_upsert_and_count(storage: Storage) -> None:
    raw = RawPost(
        channel_id=CH.id,
        msg_id=1,
        grouped_id=None,
        date=NOW,
        views=1,
        forwards=0,
        reactions=0,
        replies=0,
        preview="",
        noforwards=False,
    )
    await storage.upsert_posts([raw, raw], NOW)
    await storage.upsert_posts([raw], NOW)
    assert await storage.count_posts(CH.id) == 1


async def test_target_roundtrip(storage: Storage) -> None:
    assert await storage.get_target("Memes") is None
    await storage.set_target("Memes", -100500, NOW)
    assert await storage.get_target("Memes") == -100500
    assert await storage.delete_target("Memes") is True
    assert await storage.delete_target("Memes") is False
    assert await storage.get_target("Memes") is None


async def test_all_target_ids(storage: Storage) -> None:
    assert await storage.all_target_ids() == []
    await storage.set_target("Memes", -1009, NOW)
    await storage.set_target("News", -1008, NOW)
    assert sorted(await storage.all_target_ids()) == [-1009, -1008]


async def test_folder_roundtrip_and_lookup(storage: Storage) -> None:
    cfg = FolderConfig(min_age_h=24, period_h=6, batch_size=5)
    folder = await storage.create_folder("😊mEm", cfg, NOW)
    assert folder.id == 1
    assert folder.config == cfg
    assert folder.next_run_at == NOW and folder.created_at == NOW
    assert folder.last_fetch_at is None and folder.fetch_added == 0
    assert await storage.get_folder(1) == folder
    assert await storage.get_folder(2) is None
    assert await storage.find_folder("😊mEm") == folder
    assert await storage.find_folder("mem") == folder
    assert await storage.find_folder("MEM") == folder
    assert await storage.find_folder("memes") is None
    other = await storage.create_folder("News", FolderConfig(), NOW)
    assert await storage.list_folders() == [folder, other]


async def test_create_folder_rejects_duplicate_name(storage: Storage) -> None:
    await storage.create_folder("Memes", FolderConfig(), NOW)
    with pytest.raises(aiosqlite.IntegrityError):
        await storage.create_folder("Memes", FolderConfig(), NOW)


async def test_update_folder_config_and_state(storage: Storage) -> None:
    folder = await storage.create_folder("Memes", FolderConfig(), NOW)
    await storage.update_folder_config(folder.id, FolderConfig(batch_size=3, scoring="raw"))
    folder.last_fetch_at = NOW
    folder.last_batch_at = NOW + timedelta(seconds=1)
    folder.last_error = "boom"
    folder.fetch_msg_id = 7
    folder.fetch_added = 3
    folder.button_msg_id = 9
    folder.button_text = "footer"
    await storage.update_folder_state(folder)
    await storage.set_next_run(folder.id, NOW + timedelta(hours=6))
    stored = await storage.get_folder(folder.id)
    assert stored is not None
    assert stored.config == FolderConfig(batch_size=3, scoring="raw")
    assert stored.next_run_at == NOW + timedelta(hours=6)
    assert (stored.last_fetch_at, stored.last_batch_at, stored.last_error) == (
        NOW,
        NOW + timedelta(seconds=1),
        "boom",
    )
    assert (stored.fetch_msg_id, stored.fetch_added) == (7, 3)
    assert (stored.button_msg_id, stored.button_text) == (9, "footer")


async def test_update_folder_state_does_not_touch_config_or_schedule(storage: Storage) -> None:
    folder = await storage.create_folder("Memes", FolderConfig(), NOW)
    stale = await storage.get_folder(folder.id)
    assert stale is not None
    await storage.update_folder_config(folder.id, FolderConfig(batch_size=3))
    await storage.set_next_run(folder.id, NOW + timedelta(hours=1))
    stale.last_error = "x"
    await storage.update_folder_state(stale)
    fresh = await storage.get_folder(folder.id)
    assert fresh is not None
    assert fresh.config.batch_size == 3
    assert fresh.next_run_at == NOW + timedelta(hours=1)
    assert fresh.last_error == "x"


async def test_due_folders(storage: Storage) -> None:
    a = await storage.create_folder("A", FolderConfig(), NOW)
    b = await storage.create_folder("B", FolderConfig(), NOW)
    await storage.set_next_run(a.id, NOW + timedelta(minutes=1))
    assert [f.id for f in await storage.due_folders(NOW)] == [b.id]
    assert [f.id for f in await storage.due_folders(NOW + timedelta(minutes=1))] == [b.id, a.id]


async def test_delete_folder_cascades_queue(storage: Storage) -> None:
    folder = await storage.create_folder("Memes", FolderConfig(), NOW)
    await storage.enqueue(folder.id, [scored(1, 1.0)], NOW)
    await storage.delete_folder(folder.id)
    assert await storage.get_folder(folder.id) is None
    assert await storage.count_items(folder.id, ItemState.PENDING) == 0


async def test_folder_for_target(storage: Storage) -> None:
    folder = await storage.create_folder("Memes", FolderConfig(), NOW)
    assert await storage.folder_for_target(-100500) is None
    await storage.set_target("Memes", -100500, NOW)
    await storage.set_target("Gone", -100600, NOW)  # mapping without a folder row
    assert await storage.folder_for_target(-100500) == folder
    assert await storage.folder_for_target(-100600) is None


async def test_enqueue_inserts_updates_pending_and_skips_sent(storage: Storage) -> None:
    folder = await storage.create_folder("Memes", FolderConfig(), NOW)
    assert await storage.enqueue(folder.id, [scored(10, 0.5), scored(20, 0.2)], NOW) == 2
    items = await storage.pending_items(folder.id)
    assert [(i.msg_ids, i.score, i.state) for i in items] == [
        ((10, 11), 0.5, ItemState.PENDING),
        ((20, 21), 0.2, ItemState.PENDING),
    ]
    assert items[0].queued_at == NOW and items[0].preview == "hi"
    items[1].state = ItemState.SENT
    items[1].delivery = Delivery.FORWARD
    items[1].sent_msg_id = 77
    items[1].sent_at = NOW
    await storage.update_item(items[1])
    later = NOW + timedelta(hours=6)
    added = await storage.enqueue(
        folder.id, [scored(10, 0.9, engagement=9), scored(20, 0.9), scored(30, 0.1)], later
    )
    assert added == 1
    pending = await storage.pending_items(folder.id)
    assert [(i.msg_id, i.score, i.engagement) for i in pending] == [(10, 0.9, 9), (30, 0.1, 5)]
    assert pending[0].queued_at == NOW  # updated, not re-queued
    sent = await storage.batch_items(folder.id, NOW)
    assert [(i.msg_id, i.score, i.sent_msg_id) for i in sent] == [(20, 0.2, 77)]


async def test_enqueue_is_per_folder(storage: Storage) -> None:
    a = await storage.create_folder("A", FolderConfig(), NOW)
    b = await storage.create_folder("B", FolderConfig(), NOW)
    assert await storage.enqueue(a.id, [scored(1, 1.0)], NOW) == 1
    assert await storage.enqueue(b.id, [scored(1, 1.0)], NOW) == 1
    assert await storage.count_pending(a.id) == 1 and await storage.count_pending(b.id) == 1


async def test_pending_items_order_and_limit(storage: Storage) -> None:
    folder = await storage.create_folder("Memes", FolderConfig(), NOW)
    early, late = NOW - timedelta(hours=40), NOW - timedelta(hours=30)
    await storage.enqueue(
        folder.id,
        [
            scored(1, 0.1),
            scored(2, 0.5, engagement=5, date=late),
            scored(3, 0.5, engagement=5, date=early),
            scored(4, 0.5, engagement=9),
        ],
        NOW,
    )
    assert [i.msg_id for i in await storage.pending_items(folder.id)] == [4, 3, 2, 1]
    assert [i.msg_id for i in await storage.pending_items(folder.id, limit=2)] == [4, 3]


async def test_prune_drops_old_posts_in_any_state_and_caps_pending(storage: Storage) -> None:
    folder = await storage.create_folder("Memes", FolderConfig(max_items=2), NOW)
    old = NOW - timedelta(days=8)
    await storage.enqueue(
        folder.id,
        [
            scored(1, 0.9, date=old),
            scored(2, 0.8, date=old),
            scored(3, 0.3),
            scored(4, 0.2),
            scored(5, 0.1),
        ],
        NOW,
    )
    sent_old = (await storage.pending_items(folder.id))[0]
    sent_old.state = ItemState.SENT
    sent_old.sent_at = NOW
    await storage.update_item(sent_old)
    await storage.prune(folder.id, folder.config, NOW)
    assert [i.msg_id for i in await storage.pending_items(folder.id)] == [3, 4]
    assert await storage.count_items(folder.id, ItemState.SENT) == 0


async def test_prune_keeps_sent_rows_within_age(storage: Storage) -> None:
    folder = await storage.create_folder("Memes", FolderConfig(max_items=1), NOW)
    await storage.enqueue(folder.id, [scored(1, 0.9), scored(2, 0.8), scored(3, 0.7)], NOW)
    first = (await storage.pending_items(folder.id))[0]
    first.state = ItemState.SENT
    first.sent_at = NOW
    await storage.update_item(first)
    await storage.prune(folder.id, folder.config, NOW)
    assert [i.msg_id for i in await storage.pending_items(folder.id)] == [2]
    assert await storage.count_items(folder.id, ItemState.SENT) == 1


async def test_update_scores_touches_pending_only(storage: Storage) -> None:
    folder = await storage.create_folder("Memes", FolderConfig(), NOW)
    await storage.enqueue(folder.id, [scored(1, 0.5), scored(2, 0.4)], NOW)
    first = (await storage.pending_items(folder.id))[0]
    first.state = ItemState.FAILED
    await storage.update_item(first)
    await storage.update_scores(folder.id, [(CH.id, 1, 9.0), (CH.id, 2, 7.0)])
    assert [(i.msg_id, i.score) for i in await storage.pending_items(folder.id)] == [(2, 7.0)]
    assert await storage.count_items(folder.id, ItemState.FAILED) == 1
