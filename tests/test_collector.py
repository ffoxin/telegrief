from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegrief.collector import Collector, digest_targets, group_albums, window
from telegrief.config import FolderConfig, FoldersConfig
from telegrief.models import ChannelInfo, RawPost
from telegrief.storage import Storage
from telegrief.tg.protocols import FolderNotFound
from tests.fakes import FakeUser

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
START, END = NOW - timedelta(hours=48), NOW - timedelta(hours=24)
CH_ID = -1001
CH = ChannelInfo(
    id=CH_ID, username="a", title="A", subscribers=100, reactions_enabled=True, noforwards=False
)


def raw(
    msg_id: int,
    *,
    channel_id: int = CH_ID,
    grouped_id: int | None = None,
    date: datetime = START + timedelta(hours=1),
    views: int = 100,
    forwards: int = 0,
    reactions: int = 5,
    preview: str = "",
    noforwards: bool = False,
) -> RawPost:
    return RawPost(
        channel_id=channel_id,
        msg_id=msg_id,
        grouped_id=grouped_id,
        date=date,
        views=views,
        forwards=forwards,
        reactions=reactions,
        replies=0,
        preview=preview,
        noforwards=noforwards,
    )


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = await Storage.open(tmp_path / "t.db")
    yield s
    await s.close()


def test_window() -> None:
    assert window(NOW, 48, 24) == (START, END)
    assert window(NOW, 24, 0) == (NOW - timedelta(hours=24), NOW)


def test_group_albums_aggregates_group() -> None:
    t = START + timedelta(hours=1)
    posts = group_albums(
        [
            raw(12, grouped_id=7, date=t + timedelta(seconds=2), views=300, reactions=0),
            raw(10, grouped_id=7, date=t, views=310, reactions=40, preview="cap"),
            raw(11, grouped_id=7, date=t + timedelta(seconds=1), views=305, reactions=2),
        ]
    )
    [album] = posts
    assert album.msg_ids == (10, 11, 12)
    assert album.reactions == 42
    assert album.views == 310
    assert album.date == t
    assert album.preview == "cap"


def test_group_albums_keeps_singletons_in_order() -> None:
    posts = group_albums([raw(3), raw(1, grouped_id=9), raw(2, grouped_id=9), raw(4)])
    assert [p.msg_ids for p in posts] == [(3,), (1, 2), (4,)]


def test_group_albums_noforwards_if_any_member() -> None:
    [album] = group_albums([raw(1, grouped_id=1), raw(2, grouped_id=1, noforwards=True)])
    assert album.noforwards is True


async def test_collect_filters_window_and_stores_posts(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = [CH_ID]
    user.channels[CH_ID] = CH
    user.posts[CH_ID] = [
        raw(1, date=START - timedelta(seconds=1)),
        raw(2, date=START),
        raw(3, date=END - timedelta(seconds=1)),
        raw(4, date=END),
    ]
    result = await Collector(user, storage).collect("Memes", START, END, NOW)
    assert [p.msg_id for p in result.posts] == [2, 3]
    assert result.channels == {CH_ID: CH}
    assert result.errors == []
    assert await storage.count_posts(CH_ID) == 2


async def test_collect_uses_channel_cache_within_ttl(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = [CH_ID]
    await storage.upsert_channel(CH, NOW - timedelta(hours=23))
    result = await Collector(user, storage).collect("Memes", START, END, NOW)
    assert user.channel_info_calls == 0
    assert result.channels[CH_ID] == CH


async def test_collect_refreshes_channel_after_ttl(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = [CH_ID]
    fresh = ChannelInfo(
        id=CH_ID, username="a", title="A", subscribers=200, reactions_enabled=True, noforwards=False
    )
    user.channels[CH_ID] = fresh
    await storage.upsert_channel(CH, NOW - timedelta(hours=25))
    result = await Collector(user, storage).collect("Memes", START, END, NOW)
    assert user.channel_info_calls == 1
    assert result.channels[CH_ID].subscribers == 200
    assert await storage.get_channel(CH_ID) == (fresh, NOW)


async def test_collect_skips_unavailable_channel_and_reports(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = [CH_ID, -1002]
    user.channels[CH_ID] = CH
    user.posts[CH_ID] = [raw(1)]
    result = await Collector(user, storage).collect("Memes", START, END, NOW)
    assert [p.msg_id for p in result.posts] == [1]
    assert result.errors == ["-1002: -1002"]


async def test_collect_marks_noforwards_from_channel(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = [CH_ID]
    user.channels[CH_ID] = ChannelInfo(
        id=CH_ID, username="a", title="A", subscribers=1, reactions_enabled=True, noforwards=True
    )
    user.posts[CH_ID] = [raw(1)]
    result = await Collector(user, storage).collect("Memes", START, END, NOW)
    assert result.posts[0].noforwards is True


async def test_collect_raises_for_missing_folder(storage: Storage) -> None:
    with pytest.raises(FolderNotFound):
        await Collector(FakeUser(), storage).collect("Nope", START, END, NOW)


async def test_collect_skips_channel_on_unexpected_error(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = [CH_ID, -1002]
    user.channels[CH_ID] = CH
    user.posts[CH_ID] = [raw(1)]
    user.broken.add(-1002)
    result = await Collector(user, storage).collect("Memes", START, END, NOW)
    assert [p.msg_id for p in result.posts] == [1]
    assert result.errors == ["-1002: boom"]


async def test_digest_targets_from_storage_and_yaml(storage: Storage) -> None:
    await storage.set_target("Memes", -1009, NOW)
    folders = FoldersConfig(
        folders=[FolderConfig(name="Memes"), FolderConfig(name="News", target=-1008)]
    )
    assert await digest_targets(storage, folders) == {-1009, -1008}


async def test_collect_skips_digest_channels_sitting_in_the_folder(storage: Storage) -> None:
    user = FakeUser()
    digest_id = -1009
    user.folders["Memes"] = [digest_id, CH_ID]
    user.channels[CH_ID] = CH
    user.channels[digest_id] = ChannelInfo(
        id=digest_id,
        username=None,
        title="Memes · digest",
        subscribers=1,
        reactions_enabled=True,
        noforwards=False,
    )
    post = RawPost(
        channel_id=CH_ID,
        msg_id=1,
        grouped_id=None,
        date=START + timedelta(hours=1),
        views=100,
        forwards=0,
        reactions=5,
        replies=0,
        preview="p",
        noforwards=False,
    )
    user.posts[CH_ID] = [post]
    user.posts[digest_id] = [replace(post, channel_id=digest_id)]
    collector = Collector(user, storage)
    assert await collector.source_channels("Memes", {digest_id}) == [CH_ID]
    result = await collector.collect("Memes", START, END, NOW, exclude={digest_id})
    assert {p.channel_id for p in result.posts} == {CH_ID}
    assert result.errors == []
