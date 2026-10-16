import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegrief.collector import Collector
from telegrief.config import Settings
from telegrief.delivery import Deliverer
from telegrief.digest import DigestService
from telegrief.models import (
    ChannelInfo,
    Folder,
    FolderConfig,
    ItemState,
    Post,
    RawPost,
    ScoredPost,
)
from telegrief.storage import Storage
from telegrief.targets import TargetResolver
from telegrief.tg.protocols import SessionDead
from tests.fakes import FakeBot, FakeUser

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
CH_ID = -1001
CH = ChannelInfo(
    id=CH_ID, username="a", title="A", subscribers=100, reactions_enabled=True, noforwards=False
)
# Message ids in the digest channel after the first fetch of N>=2 posts with batch 2:
# note=1, posts=2,3, footer=4.
NOTE, FOOTER = 1, 4


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class Deferred:
    def __init__(self) -> None:
        self.calls: list[tuple[float, Callable[[], Awaitable[None]]]] = []

    def __call__(self, delay: float, callback: Callable[[], Awaitable[None]]) -> None:
        self.calls.append((delay, callback))

    async def fire_all(self) -> None:
        calls, self.calls = self.calls, []
        for _, callback in calls:
            await callback()


class Harness:
    """Service over fakes. Advance the clock between batches: sent_at is the batch key."""

    def __init__(self, storage: Storage, tmp_path: Path) -> None:
        self.user = FakeUser()
        self.bot = FakeBot()
        self.user.bot = self.bot
        self.storage = storage
        self.clock = Clock()
        self.defer = Deferred()
        self.fatal: list[Exception] = []
        self.settings = Settings(
            _secrets_dir=None,
            tg_api_id=1,
            tg_api_hash="h",
            bot_token="t",
            data_dir=tmp_path,
            auto_next_min_interval_s=120,
        )
        self.collector = Collector(self.user, storage)
        self.targets = TargetResolver(self.user, storage, self.bot.username)
        self.deliverer = Deliverer(self.user, self.bot, self.settings.max_copy_bytes)
        self.service = DigestService(
            user=self.user,
            bot=self.bot,
            storage=storage,
            collector=self.collector,
            targets=self.targets,
            deliverer=self.deliverer,
            settings=self.settings,
            clock=self.clock,
            defer=self.defer,
            on_fatal=self.fatal.append,
        )
        self.service.bind()
        self.user.folders["Memes"] = [CH_ID]
        self.user.channels[CH_ID] = CH
        self.folder: Folder

    def add_posts(self, n: int, *, start: int = 1, top: int = 100, age_h: int = 30) -> None:
        """Posts start..start+n-1 with reactions top-i (so lower ids score higher)."""
        self.user.posts[CH_ID] = [
            RawPost(
                channel_id=CH_ID,
                msg_id=i,
                grouped_id=None,
                date=NOW - timedelta(hours=age_h),
                views=1000,
                forwards=0,
                reactions=top - i,
                replies=0,
                preview=f"p{i}",
                noforwards=False,
            )
            for i in range(start, start + n)
        ]

    async def fetch(self, **kwargs: object) -> str:
        return await self.service.fetch(self.folder.id, **kwargs)  # type: ignore[arg-type]

    async def reload(self) -> Folder:
        folder = await self.storage.get_folder(self.folder.id)
        assert folder is not None
        return folder

    @property
    def target(self) -> int:
        return next(iter(self.bot.messages))

    @property
    def fid(self) -> int:
        return self.folder.id


async def make(storage: Storage, tmp_path: Path, **cfg: object) -> Harness:
    h = Harness(storage, tmp_path)
    cfg.setdefault("batch_size", 2)
    h.folder = await storage.create_folder("Memes", FolderConfig(**cfg), NOW)  # type: ignore[arg-type]
    return h


def scored(msg_id: int, reactions: int) -> ScoredPost:
    post = Post(
        channel_id=CH_ID,
        msg_ids=(msg_id,),
        date=NOW - timedelta(hours=30),
        views=1000,
        forwards=0,
        reactions=reactions,
        replies=0,
        preview="",
        noforwards=False,
    )
    return ScoredPost(post, CH, reactions, reactions / 1000)


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = await Storage.open(tmp_path / "t.db")
    yield s
    await s.close()


@pytest.fixture
async def h(storage: Storage, tmp_path: Path) -> Harness:
    return await make(storage, tmp_path)


# ---- fetch -------------------------------------------------------------------


async def test_fetch_posts_note_and_first_batch(h: Harness) -> None:
    h.add_posts(5)
    assert await h.fetch() == "✅ Memes: +5 · 5 queued"
    assert h.user.created == [("Memes · digest", "telegrief_bot")]
    target = h.target
    assert h.user.folder_pins == [("Memes", target)]
    assert h.bot.messages[target][0] == (NOTE, "📥 +5 · 5 queued", h.fid)
    assert h.user.forwarded == [(CH_ID, [1]), (CH_ID, [2])]
    footer_id, footer, next_id = h.bot.last(target)
    assert footer_id == FOOTER and next_id == h.fid
    assert footer.splitlines()[-1] == "queued 3"
    assert len(footer.splitlines()) == 3
    assert h.bot.edits == [(target, NOTE, "📥 +5 · 5 queued", None)]  # note lost its button
    folder = await h.reload()
    assert folder.last_fetch_at == NOW and folder.last_batch_at == NOW
    assert (folder.fetch_msg_id, folder.fetch_added) == (None, 0)
    assert (folder.button_msg_id, folder.button_text) == (FOOTER, footer)
    assert [i.sent_msg_id for i in await h.storage.batch_items(h.fid, NOW)] == [2, 3]


async def test_fetch_drops_posts_with_skip_phrases(h: Harness) -> None:
    await h.storage.add_skip("#реклама")
    h.add_posts(5)
    h.user.posts[CH_ID][1] = replace(h.user.posts[CH_ID][1], text="Купи слона #РЕКЛАМА сегодня")
    assert await h.fetch() == "✅ Memes: +4 · 4 queued · 🚫 1 skipped"
    assert [ids for _, ids in h.user.forwarded] == [[1], [3]]  # first batch, best first
    assert {i.msg_id for i in await h.storage.pending_items(h.fid)} == {4, 5}


async def test_fetch_window_from_reference_time_and_overrides(h: Harness) -> None:
    h.add_posts(1)  # 30h old: inside [now-48h, now-24h)
    assert await h.fetch(at=NOW - timedelta(hours=12)) == "✅ Memes: +0 · 0 queued"
    assert h.bot.messages == {}
    assert await h.fetch(from_h=31, to_h=29) == "✅ Memes: +1 · 1 queued"
    assert await h.fetch(from_h=24, to_h=24) == "❌ Memes: empty window"


async def test_fetch_with_nothing_new_is_silent(h: Harness) -> None:
    assert await h.fetch() == "✅ Memes: +0 · 0 queued"
    assert h.bot.messages == {}
    assert h.user.created  # the digest channel still gets created


async def test_second_fetch_while_unread_posts_then_edits_note(h: Harness) -> None:
    h.add_posts(5)
    await h.fetch()
    target = h.target
    h.add_posts(3, start=6)
    assert await h.fetch() == "✅ Memes: +3 · 6 queued"
    assert h.bot.messages[target][-1] == (5, "📥 +3 · 6 queued", h.fid)
    assert h.bot.edits[-1] == (target, FOOTER, h.bot.messages[target][1][1], None)
    assert len(h.user.forwarded) == 2  # batch unread: nothing sent
    h.add_posts(2, start=9)
    assert await h.fetch() == "✅ Memes: +2 · 8 queued"
    assert len(h.bot.messages[target]) == 3  # no new message
    assert h.bot.edits[-1] == (target, 5, "📥 +5 · 8 queued", h.fid)
    folder = await h.reload()
    assert (folder.fetch_msg_id, folder.fetch_added) == (5, 5)
    assert (folder.button_msg_id, folder.button_text) == (5, "📥 +5 · 8 queued")


async def test_note_edit_failure_posts_new_note(h: Harness) -> None:
    h.add_posts(5)
    await h.fetch()
    target = h.target
    h.add_posts(3, start=6)
    await h.fetch()
    h.bot.bad_edits.add(5)  # owner deleted the note
    h.add_posts(2, start=9)
    assert await h.fetch() == "✅ Memes: +2 · 8 queued"
    assert h.bot.messages[target][-1] == (6, "📥 +5 · 8 queued", h.fid)
    folder = await h.reload()
    assert folder.fetch_msg_id == 6 and folder.button_msg_id == 6


async def test_batch_clears_note_so_next_fetch_posts_new_one(h: Harness) -> None:
    h.add_posts(5)
    await h.fetch()
    target = h.target
    h.add_posts(1, start=6)
    await h.fetch()  # note 5
    h.clock.advance(120)
    await h.user.emit_read(target, 3)  # first batch read: next batch goes out
    assert h.user.forwarded[2:] == [(CH_ID, [3]), (CH_ID, [4])]
    assert h.bot.edits[-1] == (target, 5, "📥 +1 · 4 queued", None)  # note lost its button
    h.add_posts(1, start=7)
    await h.fetch()
    assert h.bot.messages[target][-1][1] == "📥 +1 · 3 queued"
    assert h.bot.messages[target][-1][0] == 9  # note=5, posts=6,7, footer=8, new note=9


async def test_batch_after_read_takes_best_of_whole_queue(h: Harness) -> None:
    h.add_posts(5)
    await h.fetch()
    target = h.target
    h.add_posts(1, start=6, top=300)  # reactions 294: best in the queue
    await h.fetch()
    h.clock.advance(120)
    await h.user.emit_read(target, 3)
    assert h.user.forwarded == [(CH_ID, [1]), (CH_ID, [2]), (CH_ID, [6]), (CH_ID, [3])]
    assert h.bot.last(target)[1].splitlines()[-1] == "queued 2"


async def test_fetch_mentions_skipped_channels(h: Harness) -> None:
    h.add_posts(1)
    h.user.folders["Memes"].append(-1002)
    assert await h.fetch() == "✅ Memes: +1 · 1 queued · ⚠️ skipped: -1002: -1002"


async def test_fetch_all_runs_every_folder(h: Harness) -> None:
    h.add_posts(1)
    await h.storage.create_folder("News", FolderConfig(), NOW)
    h.user.folders["News"] = []
    assert await h.service.fetch_all() == ["✅ Memes: +1 · 1 queued", "✅ News: +0 · 0 queued"]


async def test_fetch_unknown_folder(h: Harness) -> None:
    assert (await h.service.fetch(99)).startswith("❌ unknown folder")


# ---- failures ------------------------------------------------------------------


async def test_scheduled_failure_dms_once_until_error_changes(h: Harness) -> None:
    del h.user.folders["Memes"]
    first = await h.fetch(scheduled=True)
    assert first.startswith("❌ Memes:")
    assert await h.fetch(scheduled=True) == first
    assert h.bot.owner_messages == [first]
    folder = await h.reload()
    assert folder.last_error == first.removeprefix("❌ Memes: ")
    h.user.folders["Memes"] = [CH_ID]
    await h.fetch(scheduled=True)
    assert (await h.reload()).last_error is None
    del h.user.folders["Memes"]
    await h.fetch(scheduled=True)
    assert h.bot.owner_messages == [first, first]


async def test_manual_failure_replies_without_dm(h: Harness) -> None:
    del h.user.folders["Memes"]
    assert (await h.fetch()).startswith("❌ Memes:")
    assert h.bot.owner_messages == []
    assert (await h.reload()).last_error is None


async def test_fetch_reports_unusable_target(h: Harness) -> None:
    h.add_posts(2)
    h.bot.bad_targets.add(-1009000000001)
    result = await h.fetch(scheduled=True)
    assert result.startswith("❌ Memes:")
    assert h.bot.owner_messages == [result]


async def test_session_dead_triggers_fatal_hook(h: Harness) -> None:
    h.user.dead = True
    result = await h.fetch()
    assert result.startswith("💀 Memes:")
    assert h.bot.owner_messages == [result]
    assert len(h.fatal) == 1 and isinstance(h.fatal[0], SessionDead)


async def test_item_failure_does_not_stop_batch(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.add_posts(3)
    original = h.deliverer.deliver

    async def flaky(target: int, item: object, channel: object) -> object:
        if getattr(item, "msg_id", None) == 1:
            raise RuntimeError("boom")
        return await original(target, item, channel)  # type: ignore[arg-type]

    monkeypatch.setattr(h.deliverer, "deliver", flaky)
    await h.fetch()
    assert await h.storage.count_items(h.fid, ItemState.FAILED) == 1
    assert await h.storage.count_items(h.fid, ItemState.SENT) == 1
    assert h.bot.last(h.target)[1].splitlines() == [
        "#1 · 9.8% · 98👍 / 1.0k👁 · @a",
        "queued 1",
    ]


# ---- read tracking ---------------------------------------------------------------


async def test_on_read_below_marker_does_nothing(h: Harness) -> None:
    h.add_posts(5)
    await h.fetch()
    h.clock.advance(600)
    await h.user.emit_read(h.target, 2)
    assert len(h.user.forwarded) == 2
    assert h.defer.calls == []


async def test_on_read_at_marker_sends_next_batch(h: Harness) -> None:
    h.add_posts(5)
    await h.fetch()
    target = h.target
    h.clock.advance(120)
    await h.user.emit_read(target, 3)
    assert h.user.forwarded == [(CH_ID, [1]), (CH_ID, [2]), (CH_ID, [3]), (CH_ID, [4])]
    assert h.bot.last(target)[1].splitlines()[-1] == "queued 1"
    assert h.bot.edits[-1] == (target, FOOTER, h.bot.messages[target][1][1], None)


async def test_on_read_sends_next_once_90_percent_of_batch_is_read(
    storage: Storage, tmp_path: Path
) -> None:
    h = await make(storage, tmp_path, batch_size=10)
    h.add_posts(12)
    await h.fetch()
    target = h.target
    assert len(h.user.forwarded) == 10
    h.clock.advance(120)
    await h.user.emit_read(target, 9)  # note=1, posts=2..11: 8 of 10 read
    assert len(h.user.forwarded) == 10
    await h.user.emit_read(target, 10)  # 9 of 10 read
    assert len(h.user.forwarded) == 12


async def test_on_read_within_interval_defers_instead_of_dropping(h: Harness) -> None:
    h.add_posts(5)
    await h.fetch()
    target = h.target
    h.clock.advance(30)
    await h.user.emit_read(target, 3)
    assert len(h.user.forwarded) == 2
    assert [d for d, _ in h.defer.calls] == [90.0]
    await h.user.emit_read(target, 3)  # second event while recheck pending: no new timer
    assert len(h.defer.calls) == 1
    h.clock.advance(90)
    await h.defer.fire_all()
    assert len(h.user.forwarded) == 4


async def test_on_read_ignores_unknown_channel(h: Harness) -> None:
    h.add_posts(5)
    await h.fetch()
    h.clock.advance(600)
    await h.user.emit_read(-100999, 10_000)
    assert len(h.user.forwarded) == 2


async def test_concurrent_read_events_send_one_batch(h: Harness) -> None:
    h.add_posts(5)
    await h.fetch()
    target = h.target
    h.clock.advance(120)
    await asyncio.gather(h.user.emit_read(target, 3), h.user.emit_read(target, 3))
    assert len(h.user.forwarded) == 4


async def test_next_button_bypasses_pacing(h: Harness) -> None:
    h.add_posts(5)
    await h.fetch()
    h.clock.advance(1)
    await h.bot.press_next(h.fid)
    assert len(h.user.forwarded) == 4


async def test_last_batch_leaves_no_button(h: Harness) -> None:
    h.add_posts(3)
    await h.fetch()
    target = h.target
    h.clock.advance(1)
    await h.bot.press_next(h.fid)
    _, footer, next_id = h.bot.last(target)
    assert footer.splitlines()[-1] == "✅ queue empty"
    assert next_id is None
    folder = await h.reload()
    assert folder.button_msg_id is None
    await h.bot.press_next(h.fid)  # empty queue: nothing happens
    assert len(h.user.forwarded) == 3


async def test_next_button_with_dead_target_reports(h: Harness) -> None:
    h.add_posts(5)
    await h.fetch()
    h.bot.bad_targets.add(h.target)
    await h.bot.press_next(h.fid)
    assert h.bot.owner_messages[-1].startswith("❌ Memes: digest channel unusable")


async def test_prune_before_batch_drops_aged_posts(storage: Storage, tmp_path: Path) -> None:
    h = await make(storage, tmp_path, min_age_h=0, period_h=12, max_age_d=1)
    h.add_posts(3, age_h=1)
    await h.fetch()
    assert len(h.user.forwarded) == 2
    h.clock.advance(25 * 3600)
    await h.bot.press_next(h.fid)
    assert len(h.user.forwarded) == 2
    assert await h.storage.count_pending(h.fid) == 0


async def test_catch_up_sends_next_when_marker_already_past(h: Harness) -> None:
    h.add_posts(5)
    await h.fetch()
    h.user.read_markers[h.target] = 3
    h.clock.advance(600)
    await h.service.catch_up()
    assert len(h.user.forwarded) == 4


async def test_catch_up_sends_first_batch_for_never_sent_folder(h: Harness) -> None:
    await h.storage.set_target("Memes", -100500, NOW)
    await h.storage.enqueue(h.fid, [scored(1, 50), scored(2, 40), scored(3, 30)], NOW)
    await h.service.catch_up()
    assert h.user.forwarded == [(CH_ID, [1]), (CH_ID, [2])]
    assert h.bot.last(-100500)[1].splitlines()[-1] == "queued 1"


async def test_catch_up_skips_folders_without_channel(h: Harness) -> None:
    await h.service.catch_up()
    assert h.user.forwarded == [] and h.bot.owner_messages == []


async def test_catch_up_survives_marker_failure(h: Harness) -> None:
    h.add_posts(5)
    await h.fetch()
    h.user.marker_fails.add(h.target)
    await h.service.catch_up()  # must not raise
    assert h.bot.owner_messages and "channel gone" in h.bot.owner_messages[-1]


async def test_rescore_updates_pending_from_cached_channel(h: Harness) -> None:
    h.add_posts(5)
    await h.fetch()
    assert [i.score for i in await h.storage.pending_items(h.fid)] == [0.097, 0.096, 0.095]
    await h.storage.update_folder_config(h.fid, FolderConfig(batch_size=2, scoring="raw"))
    assert await h.service.rescore(h.fid) == 3
    assert [i.score for i in await h.storage.pending_items(h.fid)] == [97.0, 96.0, 95.0]
    cfg = FolderConfig(batch_size=2, scoring="per_subscriber", min_denominator=50)
    await h.storage.update_folder_config(h.fid, cfg)
    await h.service.rescore(h.fid)
    scores = [i.score for i in await h.storage.pending_items(h.fid)]
    assert scores == pytest.approx([970.0, 960.0, 950.0])


async def test_bind_wires_both_gateways(h: Harness) -> None:
    assert h.user._read_handler is not None
    assert h.bot._next_handler is not None
