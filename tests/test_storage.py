from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegrief.models import (
    ChannelInfo,
    Delivery,
    DigestStatus,
    ItemState,
    Post,
    RawPost,
    ScoredPost,
)
from telegrief.storage import Storage

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
CH = ChannelInfo(
    id=-1001, username="a", title="A", subscribers=10, reactions_enabled=True, noforwards=False
)


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = await Storage.open(tmp_path / "t.db")
    yield s
    await s.close()


def scored(msg_id: int, score: float, channel: ChannelInfo = CH) -> ScoredPost:
    post = Post(
        channel_id=channel.id,
        msg_ids=(msg_id, msg_id + 1),
        date=NOW - timedelta(hours=30),
        views=100,
        forwards=0,
        reactions=5,
        replies=0,
        preview="hi",
        noforwards=False,
    )
    return ScoredPost(post, channel, 5, score)


async def test_migrate_is_idempotent(storage: Storage) -> None:
    assert await storage.migrate() == 1
    assert await storage.migrate() == 1


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


async def test_create_digest_stores_items_in_rank_order(storage: Storage) -> None:
    start, end = NOW - timedelta(hours=48), NOW - timedelta(hours=24)
    digest = await storage.create_digest(
        "Memes", -100500, start, end, [scored(10, 0.5), scored(20, 0.2)], NOW
    )
    assert digest.id == 1
    assert digest.total == 2
    assert digest.status == DigestStatus.OPEN
    assert digest.window_start == start and digest.window_end == end
    items = await storage.pending_items(digest.id, limit=10)
    assert [(i.rank, i.msg_ids, i.score) for i in items] == [
        (1, (10, 11), 0.5),
        (2, (20, 21), 0.2),
    ]
    assert items[0].channel_id == CH.id
    assert items[0].preview == "hi"
    assert items[0].engagement == 5 and items[0].views == 100
    assert await storage.get_digest(digest.id) == digest
    assert await storage.get_digest(999) is None


async def test_create_digest_closes_older_open_digest_for_same_target(storage: Storage) -> None:
    start, end = NOW - timedelta(hours=48), NOW - timedelta(hours=24)
    first = await storage.create_digest("Memes", -100500, start, end, [scored(1, 1.0)], NOW)
    other = await storage.create_digest("News", -100600, start, end, [scored(1, 1.0)], NOW)
    second = await storage.create_digest("Memes", -100500, start, end, [scored(2, 1.0)], NOW)
    first_now = await storage.get_digest(first.id)
    assert first_now is not None and first_now.status == DigestStatus.DONE
    open_memes = await storage.open_digest_for_target(-100500)
    assert open_memes is not None and open_memes.id == second.id
    assert {d.id for d in await storage.open_digests()} == {other.id, second.id}
    latest = await storage.latest_digest("Memes")
    assert latest is not None and latest.id == second.id
    assert await storage.latest_digest("Nope") is None


async def test_pending_items_limit_and_item_updates(storage: Storage) -> None:
    start, end = NOW - timedelta(hours=48), NOW - timedelta(hours=24)
    digest = await storage.create_digest(
        "Memes", -100500, start, end, [scored(i, 1.0 / i) for i in range(1, 6)], NOW
    )
    batch = await storage.pending_items(digest.id, limit=2)
    assert [i.rank for i in batch] == [1, 2]
    assert await storage.max_sent_msg_id(digest.id) is None
    batch[0].state = ItemState.SENT
    batch[0].delivery = Delivery.FORWARD
    batch[0].sent_msg_id = 77
    batch[0].sent_at = NOW
    await storage.update_item(batch[0])
    batch[1].state = ItemState.FAILED
    await storage.update_item(batch[1])
    assert [i.rank for i in await storage.pending_items(digest.id, limit=10)] == [3, 4, 5]
    assert await storage.count_items(digest.id, ItemState.SENT) == 1
    assert await storage.count_items(digest.id, ItemState.FAILED) == 1
    assert await storage.count_items(digest.id, ItemState.PENDING) == 3
    assert await storage.max_sent_msg_id(digest.id) == 77


async def test_update_digest(storage: Storage) -> None:
    start, end = NOW - timedelta(hours=48), NOW - timedelta(hours=24)
    digest = await storage.create_digest("Memes", -100500, start, end, [scored(1, 1.0)], NOW)
    digest.title_msg_id = 5
    digest.footer_msg_id = 9
    digest.footer_text = "footer"
    digest.last_batch_at = NOW
    digest.status = DigestStatus.DONE
    await storage.update_digest(digest)
    assert await storage.get_digest(digest.id) == digest


async def test_update_digest_does_not_reopen_closed_digest(storage: Storage) -> None:
    start, end = NOW - timedelta(hours=48), NOW - timedelta(hours=24)
    first = await storage.create_digest("Memes", -100500, start, end, [scored(1, 1.0)], NOW)
    await storage.create_digest("Memes", -100500, start, end, [scored(2, 1.0)], NOW)
    first.footer_msg_id = 9  # a batch that was in flight when the new run closed it
    await storage.update_digest(first)
    stored = await storage.get_digest(first.id)
    assert stored is not None
    assert stored.status == DigestStatus.DONE
    assert stored.footer_msg_id is None
