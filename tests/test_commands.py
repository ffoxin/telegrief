from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegrief.collector import Collector
from telegrief.commands import USAGE, CommandHandler, parse_run_args
from telegrief.config import FolderConfig, FoldersConfig, Settings
from telegrief.delivery import Deliverer
from telegrief.digest import DigestService
from telegrief.models import ChannelInfo, RawPost
from telegrief.storage import Storage
from telegrief.targets import TargetResolver
from tests.fakes import FakeBot, FakeUser

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
CH_ID = -1001
CH = ChannelInfo(
    id=CH_ID,
    username="a",
    title="Alpha",
    subscribers=2900,
    reactions_enabled=True,
    noforwards=False,
)


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = await Storage.open(tmp_path / "t.db")
    yield s
    await s.close()


class Harness:
    def __init__(self, storage: Storage, tmp_path: Path) -> None:
        self.user = FakeUser()
        self.bot = FakeBot()
        self.user.bot = self.bot
        self.storage = storage
        self.folders = FoldersConfig(
            folders=[FolderConfig(name="Memes", batch_size=2), FolderConfig(name="My News")]
        )
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
            folders=lambda: self.folders,
            clock=lambda: NOW,
        )
        self.handler = CommandHandler(
            self.service, self.user, storage, collector, targets, lambda: self.folders, lambda: NOW
        )
        self.bot.on_owner_command(self.handler.handle)
        self.user.folders["Memes"] = [CH_ID]
        self.user.folders["My News"] = []
        self.user.channels[CH_ID] = CH
        self.user.posts[CH_ID] = [
            RawPost(
                channel_id=CH_ID,
                msg_id=i,
                grouped_id=None,
                date=NOW - timedelta(hours=30),
                views=100,
                forwards=0,
                reactions=10 - i,
                replies=0,
                preview="",
                noforwards=False,
            )
            for i in range(1, 4)
        ]


@pytest.fixture
def h(storage: Storage, tmp_path: Path) -> Harness:
    return Harness(storage, tmp_path)


def test_parse_run_args() -> None:
    assert parse_run_args([]) == (None, None, None)
    assert parse_run_args(["Memes"]) == ("Memes", None, None)
    assert parse_run_args(["Memes", "24"]) == ("Memes", 24, None)
    assert parse_run_args(["Memes", "24", "0"]) == ("Memes", 24, 0)
    assert parse_run_args(["24", "0"]) == (None, 24, 0)


def test_run_parses_folder_with_spaces() -> None:
    assert parse_run_args(["My", "News", "24", "0"]) == ("My News", 24, 0)


async def test_run_one_folder(h: Harness) -> None:
    assert await h.bot.command("run", ["Memes"]) == "✅ Memes: 3 posts"


async def test_run_all_folders(h: Harness) -> None:
    assert await h.bot.command("run", []) == "✅ Memes: 3 posts\n✅ My News: 0 posts"


async def test_run_accepts_text_only_name(h: Harness) -> None:
    assert await h.bot.command("run", ["memes"]) == "✅ Memes: 3 posts"
    assert await h.bot.command("run", ["my", "news", "24", "0"]) == "✅ My News: 0 posts"


async def test_reset_accepts_text_only_name(h: Harness) -> None:
    await h.bot.command("run", ["Memes"])
    assert await h.bot.command("reset", ["memes"]) == "reset Memes; next run creates a new channel"


async def test_run_unknown_folder(h: Harness) -> None:
    assert await h.bot.command("run", ["Nope"]) == "unknown folder: Nope"


async def test_run_rejects_inverted_hours(h: Harness) -> None:
    assert await h.bot.command("run", ["Memes", "0", "24"]) == "from_h must be greater than to_h"


async def test_status_none_and_open(h: Harness) -> None:
    assert await h.bot.command("status", []) == "Memes: none\nMy News: none"
    await h.bot.command("run", ["Memes"])
    assert (
        await h.bot.command("status", [])
        == "Memes: 2026-10-14 · open · shown 2/3 · pending 1\nMy News: none"
    )


async def test_folders_lists_resolved_channels(h: Harness) -> None:
    h.user.folder_titles = ["🔥 Memes", "My News"]
    text = await h.bot.command("folders", [])
    assert text == (
        "Telegram folders: 🔥 Memes · My News\nMemes (1):\n  Alpha (@a) · 2.9k\nMy News (0):"
    )


async def test_folders_reports_missing_folder(h: Harness) -> None:
    del h.user.folders["My News"]
    text = await h.bot.command("folders", [])
    assert text.splitlines()[-1] == "My News: ❌ folder not found"


async def test_reset(h: Harness) -> None:
    assert await h.bot.command("reset", []) == USAGE
    assert await h.bot.command("reset", ["Memes"]) == "no digest channel mapping for Memes"
    await h.bot.command("run", ["Memes"])
    assert await h.bot.command("reset", ["Memes"]) == "reset Memes; next run creates a new channel"


async def test_unknown_command_shows_usage(h: Harness) -> None:
    assert await h.bot.command("help", []) == USAGE
    assert await h.bot.command("start", []) == USAGE
