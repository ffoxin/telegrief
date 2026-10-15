import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegrief.collector import Collector
from telegrief.config import FolderConfig, FoldersConfig, Settings
from telegrief.delivery import Deliverer
from telegrief.digest import DigestService
from telegrief.models import ChannelInfo, DigestStatus, ItemState, RawPost
from telegrief.storage import Storage
from telegrief.targets import TargetResolver
from telegrief.tg.protocols import SessionDead
from tests.fakes import FakeBot, FakeUser

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
CH_ID = -1001
CH = ChannelInfo(
    id=CH_ID, username="a", title="A", subscribers=100, reactions_enabled=True, noforwards=False
)


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
    def __init__(self, storage: Storage, tmp_path: Path, batch_size: int = 2) -> None:
        self.user = FakeUser()
        self.bot = FakeBot()
        self.user.bot = self.bot
        self.storage = storage
        self.clock = Clock()
        self.defer = Deferred()
        self.fatal: list[Exception] = []
        self.folders = FoldersConfig(folders=[FolderConfig(name="Memes", batch_size=batch_size)])
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
            folders=lambda: self.folders,
            clock=self.clock,
            defer=self.defer,
            on_fatal=self.fatal.append,
        )
        self.service.bind()
        self.user.folders["Memes"] = [CH_ID]
        self.user.channels[CH_ID] = CH

    def add_posts(self, n: int) -> None:
        self.user.posts[CH_ID] = [
            RawPost(
                channel_id=CH_ID,
                msg_id=i,
                grouped_id=None,
                date=NOW - timedelta(hours=30),
                views=1000,
                forwards=0,
                reactions=100 - i,
                replies=0,
                preview=f"p{i}",
                noforwards=False,
            )
            for i in range(1, n + 1)
        ]

    @property
    def target(self) -> int:
        return next(iter(self.bot.messages))


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = await Storage.open(tmp_path / "t.db")
    yield s
    await s.close()


@pytest.fixture
def h(storage: Storage, tmp_path: Path) -> Harness:
    return Harness(storage, tmp_path)


async def test_run_folder_creates_digest_and_sends_first_batch(h: Harness) -> None:
    h.add_posts(5)
    result = await h.service.run_folder(h.folders.folders[0])
    assert result == "✅ Memes: 5 posts"
    assert h.user.created == [("Memes · digest", "telegrief_bot")]
    target = h.target
    texts = h.bot.texts(target)
    assert texts[0] == "📂 Memes · 2026-10-14 · 5 posts from 1 channels"
    assert h.user.forwarded == [(CH_ID, [1]), (CH_ID, [2])]
    footer_id, footer, next_id = h.bot.last(target)
    assert footer.splitlines()[-1] == "shown 2/5"
    assert next_id == 1
    digest = await h.storage.get_digest(1)
    assert digest is not None
    assert digest.status == DigestStatus.OPEN
    assert digest.footer_msg_id == footer_id
    assert digest.footer_text == footer
    assert digest.last_batch_at == NOW
    # title=1, posts=2,3, footer=4
    assert [i.sent_msg_id for i in await h.storage.batch_items(1, NOW)] == [2, 3]


async def test_window_uses_settings_and_overrides(h: Harness) -> None:
    h.add_posts(1)
    await h.service.run_folder(h.folders.folders[0])
    digest = await h.storage.get_digest(1)
    assert digest is not None
    assert digest.window_start == NOW - timedelta(hours=48)
    assert digest.window_end == NOW - timedelta(hours=24)
    h.user.posts[CH_ID][0] = replace(h.user.posts[CH_ID][0], date=NOW - timedelta(hours=1))
    result = await h.service.run_folder(h.folders.folders[0], from_h=24, to_h=0)
    assert result == "✅ Memes: 1 posts"
    digest = await h.storage.get_digest(2)
    assert digest is not None and digest.window_end == NOW


async def test_empty_digest_is_done_without_footer(h: Harness) -> None:
    await h.service.run_folder(h.folders.folders[0])
    target = h.target
    assert h.bot.texts(target) == ["📂 Memes · 2026-10-14 · no posts in window"]
    digest = await h.storage.get_digest(1)
    assert digest is not None and digest.status == DigestStatus.DONE


async def test_on_read_below_marker_does_nothing(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
    h.clock.advance(600)
    await h.user.emit_read(h.target, 2)
    assert len(h.user.forwarded) == 2
    assert h.defer.calls == []


async def test_on_read_at_marker_sends_next_batch(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
    target = h.target
    h.clock.advance(120)
    await h.user.emit_read(target, 3)
    assert h.user.forwarded == [(CH_ID, [1]), (CH_ID, [2]), (CH_ID, [3]), (CH_ID, [4])]
    assert h.bot.last(target)[1].splitlines()[-1] == "shown 4/5"
    # previous footer lost its button
    assert h.bot.edits == [(target, 4, h.bot.messages[target][1][1], None)]


async def test_on_read_sends_next_once_90_percent_of_batch_is_read(
    storage: Storage, tmp_path: Path
) -> None:
    h = Harness(storage, tmp_path, batch_size=10)
    h.add_posts(12)
    await h.service.run_folder(h.folders.folders[0])
    target = h.target
    assert len(h.user.forwarded) == 10
    h.clock.advance(120)
    await h.user.emit_read(target, 9)  # title=1, posts=2..11: 8 of 10 read
    assert len(h.user.forwarded) == 10
    await h.user.emit_read(target, 10)  # 9 of 10 read
    assert len(h.user.forwarded) == 12


async def test_on_read_within_interval_defers_instead_of_dropping(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
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
    await h.service.run_folder(h.folders.folders[0])
    h.clock.advance(600)
    await h.user.emit_read(-100999, 10_000)
    assert len(h.user.forwarded) == 2


async def test_stale_digest_ignored_after_new_run(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
    target = h.target
    h.clock.advance(24 * 3600)
    await h.service.run_folder(h.folders.folders[0])
    first = await h.storage.get_digest(1)
    assert first is not None and first.status == DigestStatus.DONE
    before = len(h.user.forwarded)
    h.clock.advance(600)
    await h.user.emit_read(target, 3)  # marker only past digest 1's batch
    assert len(h.user.forwarded) == before


async def test_next_button_bypasses_pacing(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
    await h.bot.press_next(1)
    assert len(h.user.forwarded) == 4


async def test_last_batch_marks_done(h: Harness) -> None:
    h.add_posts(3)
    await h.service.run_folder(h.folders.folders[0])
    target = h.target
    await h.bot.press_next(1)
    digest = await h.storage.get_digest(1)
    assert digest is not None and digest.status == DigestStatus.DONE
    _, footer, next_id = h.bot.last(target)
    assert footer.splitlines()[-1] == "✅ all 3 shown"
    assert next_id is None
    await h.bot.press_next(1)  # done digest: nothing happens
    assert len(h.user.forwarded) == 3


async def test_item_failure_does_not_stop_batch(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.add_posts(3)
    original = h.deliverer.deliver

    async def flaky(target: int, item: object, channel: object) -> object:
        if getattr(item, "rank", None) == 1:
            raise RuntimeError("boom")
        return await original(target, item, channel)  # type: ignore[arg-type]

    monkeypatch.setattr(h.deliverer, "deliver", flaky)
    await h.service.run_folder(h.folders.folders[0])
    assert await h.storage.count_items(1, ItemState.FAILED) == 1
    assert await h.storage.count_items(1, ItemState.SENT) == 1
    assert h.bot.last(h.target)[1].splitlines()[-1] == "shown 1/3"


async def test_run_folder_reports_failure_to_owner(h: Harness) -> None:
    del h.user.folders["Memes"]
    result = await h.service.run_folder(h.folders.folders[0])
    assert result.startswith("❌ Memes:")
    assert h.bot.owner_messages == [result]


async def test_run_folder_marks_failed_when_target_unusable(h: Harness) -> None:
    h.add_posts(2)
    h.bot.bad_targets.add(-1009000000001)
    result = await h.service.run_folder(h.folders.folders[0])
    assert result.startswith("❌ Memes:")
    digest = await h.storage.get_digest(1)
    assert digest is not None and digest.status == DigestStatus.FAILED


async def test_run_folder_mentions_skipped_channels(h: Harness) -> None:
    h.add_posts(1)
    h.user.folders["Memes"].append(-1002)
    result = await h.service.run_folder(h.folders.folders[0])
    assert result == "✅ Memes: 1 posts · ⚠️ skipped: -1002: -1002"


async def test_run_all_runs_every_folder(h: Harness) -> None:
    h.add_posts(1)
    assert await h.service.run_all() == ["✅ Memes: 1 posts"]


async def test_catch_up_sends_next_when_marker_already_past(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
    h.user.read_markers[h.target] = 3
    h.clock.advance(600)
    await h.service.catch_up()
    assert len(h.user.forwarded) == 4


async def test_send_batch_without_folder_config_is_noop(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
    h.folders = FoldersConfig(folders=[FolderConfig(name="Other")])
    assert await h.service.send_batch(1) == 0
    assert len(h.user.forwarded) == 2


async def test_bind_wires_both_gateways(h: Harness) -> None:
    assert h.user._read_handler is not None
    assert h.bot._next_handler is not None


async def test_concurrent_read_events_send_one_batch(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
    target = h.target
    h.clock.advance(120)
    await asyncio.gather(h.user.emit_read(target, 3), h.user.emit_read(target, 3))
    assert len(h.user.forwarded) == 4  # one extra batch, not two


async def test_next_button_with_dead_target_marks_failed(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
    h.bot.bad_targets.add(h.target)
    await h.bot.press_next(1)
    digest = await h.storage.get_digest(1)
    assert digest is not None and digest.status == DigestStatus.FAILED
    assert h.bot.owner_messages and h.bot.owner_messages[-1].startswith("❌ digest 1")


async def test_catch_up_survives_marker_failure(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
    h.user.marker_fails.add(h.target)
    await h.service.catch_up()  # must not raise
    digest = await h.storage.get_digest(1)
    assert digest is not None and digest.status == DigestStatus.FAILED
    assert h.bot.owner_messages and "channel gone" in h.bot.owner_messages[-1]


async def test_unexpected_error_after_create_marks_failed(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.add_posts(2)

    async def broken_send_text(*args: object, **kwargs: object) -> int:
        raise RuntimeError("sqlite is on fire")

    monkeypatch.setattr(h.bot, "send_text", broken_send_text)
    result = await h.service.run_folder(h.folders.folders[0])
    assert result.startswith("❌ Memes:")
    digest = await h.storage.get_digest(1)
    assert digest is not None and digest.status == DigestStatus.FAILED


async def test_session_dead_triggers_fatal_hook(h: Harness) -> None:
    h.user.dead = True
    result = await h.service.run_folder(h.folders.folders[0])
    assert result.startswith("💀 Memes:")
    assert h.bot.owner_messages == [result]
    assert len(h.fatal) == 1 and isinstance(h.fatal[0], SessionDead)
