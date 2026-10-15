from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegrief.collector import Collector
from telegrief.config import Settings
from telegrief.delivery import Deliverer
from telegrief.digest import DigestService
from telegrief.models import ChannelInfo, FolderConfig, RawPost
from telegrief.scheduler import Heartbeat, Ticker, latest_slot
from telegrief.storage import Storage
from telegrief.targets import TargetResolver
from tests.fakes import FakeBot, FakeUser

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
H = timedelta(hours=1)
CH_ID = -1001
CH = ChannelInfo(
    id=CH_ID, username="a", title="A", subscribers=100, reactions_enabled=True, noforwards=False
)


def test_latest_slot() -> None:
    assert latest_slot(NOW, 6 * H, NOW) == NOW
    assert latest_slot(NOW - 13 * H, 6 * H, NOW) == NOW - H
    assert latest_slot(NOW - 12 * H, 6 * H, NOW) == NOW
    assert latest_slot(NOW - 5 * H, 6 * H, NOW) == NOW - 5 * H
    with pytest.raises(ValueError):
        latest_slot(NOW + H, 6 * H, NOW)


def test_heartbeat_touch_creates_and_updates(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    hb = Heartbeat(path)
    hb.touch()
    assert path.exists()
    first = path.stat().st_mtime
    hb.touch()
    assert path.stat().st_mtime >= first


class Harness:
    def __init__(self, storage: Storage, tmp_path: Path) -> None:
        self.user = FakeUser()
        self.bot = FakeBot()
        self.user.bot = self.bot
        self.storage = storage
        self.now = NOW
        settings = Settings(
            _secrets_dir=None, tg_api_id=1, tg_api_hash="h", bot_token="t", data_dir=tmp_path
        )
        collector = Collector(self.user, storage)
        targets = TargetResolver(self.user, storage, self.bot.username)
        self.service = DigestService(
            user=self.user,
            bot=self.bot,
            storage=storage,
            collector=collector,
            targets=targets,
            deliverer=Deliverer(self.user, self.bot, settings.max_copy_bytes),
            settings=settings,
            clock=lambda: self.now,
        )
        self.ticker = Ticker(storage, self.service, clock=lambda: self.now)
        self.user.folders["Memes"] = [CH_ID]
        self.user.channels[CH_ID] = CH

    def post(self, msg_id: int, age: timedelta) -> None:
        self.user.posts.setdefault(CH_ID, []).append(
            RawPost(
                channel_id=CH_ID,
                msg_id=msg_id,
                grouped_id=None,
                date=NOW - age,
                views=1000,
                forwards=0,
                reactions=10,
                replies=0,
                preview="",
                noforwards=False,
            )
        )


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = await Storage.open(tmp_path / "t.db")
    yield s
    await s.close()


@pytest.fixture
def h(storage: Storage, tmp_path: Path) -> Harness:
    return Harness(storage, tmp_path)


async def test_tick_uses_latest_slot_and_advances_past_now(h: Harness) -> None:
    folder = await h.storage.create_folder("Memes", FolderConfig(min_age_h=24, period_h=6), NOW)
    await h.storage.set_next_run(folder.id, NOW - 13 * H)  # two slots missed; slot = NOW-1h
    h.post(1, age=26 * H)  # inside the slot window [NOW-31h, NOW-25h)
    h.post(2, age=24 * H + timedelta(minutes=30))  # inside a now-based window only
    assert await h.ticker.tick() == ["✅ Memes: +1 · 1 queued"]
    assert h.user.forwarded == [(CH_ID, [1])]
    stored = await h.storage.get_folder(folder.id)
    assert stored is not None and stored.next_run_at == NOW + 5 * H


async def test_tick_skips_folders_not_due(h: Harness) -> None:
    folder = await h.storage.create_folder("Memes", FolderConfig(), NOW)
    await h.storage.set_next_run(folder.id, NOW + timedelta(minutes=1))
    assert await h.ticker.tick() == []
    assert h.user.created == []


async def test_tick_advances_even_when_fetch_fails(h: Harness) -> None:
    folder = await h.storage.create_folder("Memes", FolderConfig(), NOW)
    del h.user.folders["Memes"]
    results = await h.ticker.tick()
    assert results and results[0].startswith("❌ Memes:")
    assert h.bot.owner_messages == results
    stored = await h.storage.get_folder(folder.id)
    assert stored is not None and stored.next_run_at == NOW + 24 * H
    assert stored.last_error is not None


async def test_tick_runs_every_due_folder_in_schedule_order(h: Harness) -> None:
    await h.storage.create_folder("Memes", FolderConfig(), NOW)
    news = await h.storage.create_folder("News", FolderConfig(), NOW)
    await h.storage.set_next_run(news.id, NOW - H)
    h.user.folders["News"] = []
    assert await h.ticker.tick() == ["✅ News: +0 · 0 queued", "✅ Memes: +0 · 0 queued"]
    assert await h.ticker.tick() == []


async def test_tick_keeps_going_after_one_folder_fails(h: Harness) -> None:
    await h.storage.create_folder("Broken", FolderConfig(), NOW)
    await h.storage.create_folder("Memes", FolderConfig(), NOW)
    h.post(1, age=30 * H)
    results = await h.ticker.tick()
    assert results[0].startswith("❌ Broken:")
    assert results[1] == "✅ Memes: +1 · 1 queued"
    assert [f.next_run_at for f in await h.storage.list_folders()] == [NOW + 24 * H] * 2
