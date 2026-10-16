import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from telegrief.collector import Collector
from telegrief.commands import (
    USAGE,
    CommandHandler,
    apply_pairs,
    help_for,
    menu,
    next_occurrence,
    parse_folder_args,
    parse_run_args,
)
from telegrief.config import Settings
from telegrief.delivery import Deliverer
from telegrief.digest import DigestService
from telegrief.models import ChannelInfo, FolderConfig, RawPost
from telegrief.storage import Storage
from telegrief.targets import TargetResolver
from tests.fakes import FakeBot, FakeUser

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
UTC_TZ = ZoneInfo("UTC")
CH_ID = -1001
CH = ChannelInfo(
    id=CH_ID,
    username="a",
    title="Alpha",
    subscribers=2900,
    reactions_enabled=True,
    noforwards=False,
)
MEMES_LINE = (
    "Memes · 24/24h · batch 2 · per_view/200 · max 1000 · 7d · queued 0 · next 2026-10-15 02:00"
)
NEWS_LINE = (
    "My News · 24/24h · batch 10 · per_view/200 · max 1000 · 7d · queued 0 · next 2026-10-15 02:00"
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
            clock=lambda: NOW,
        )
        self.handler = CommandHandler(
            self.service, self.user, storage, collector, targets, UTC_TZ, lambda: NOW
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
async def h(storage: Storage, tmp_path: Path) -> Harness:
    h = Harness(storage, tmp_path)
    await storage.create_folder("Memes", FolderConfig(batch_size=2), NOW)
    await storage.create_folder("My News", FolderConfig(), NOW)
    return h


# ---- parsers ---------------------------------------------------------------------


def test_parse_run_args() -> None:
    assert parse_run_args([]) == (None, None, None)
    assert parse_run_args(["Memes"]) == ("Memes", None, None)
    assert parse_run_args(["Memes", "24"]) == ("Memes", 24, None)
    assert parse_run_args(["Memes", "24", "0"]) == ("Memes", 24, 0)
    assert parse_run_args(["24", "0"]) == (None, 24, 0)
    assert parse_run_args(["My", "News", "24", "0"]) == ("My News", 24, 0)


def test_parse_folder_args() -> None:
    assert parse_folder_args(["Memes"]) == ("Memes", {}, False)
    assert parse_folder_args(["Memes", "min_age=24", "period=6"]) == (
        "Memes",
        {"min_age": "24", "period": "6"},
        False,
    )
    assert parse_folder_args(["My", "News", "batch=5"]) == ("My News", {"batch": "5"}, False)
    assert parse_folder_args(["My", "News", "off"]) == ("My News", {}, True)
    assert parse_folder_args(["Memes", "off"]) == ("Memes", {}, True)


def test_parse_folder_args_rejects_bad_syntax() -> None:
    with pytest.raises(ValueError, match="name required"):
        parse_folder_args([])
    with pytest.raises(ValueError, match="reserved"):
        parse_folder_args(["off"])
    assert parse_folder_args(["Memes", "batch"]) == ("Memes batch", {}, False)  # a name
    with pytest.raises(ValueError, match="key=value"):
        parse_folder_args(["Memes", "batch="])
    with pytest.raises(ValueError, match="off takes no settings"):
        parse_folder_args(["Memes", "off", "batch=1"])


def test_apply_pairs() -> None:
    cfg = apply_pairs(FolderConfig(), {"batch": "20", "min_age": "0", "scoring": "raw"})
    assert (cfg.batch_size, cfg.min_age_h, cfg.scoring) == (20, 0, "raw")
    assert cfg.period_h == 24  # untouched
    with pytest.raises(ValueError, match="unknown key: batchsize, foo"):
        apply_pairs(FolderConfig(), {"foo": "1", "batchsize": "2"})
    with pytest.raises(ValueError, match=r"^min_age_h:"):
        apply_pairs(FolderConfig(), {"min_age": "abc"})
    with pytest.raises(ValueError, match="max_age"):
        apply_pairs(FolderConfig(), {"min_age": "24", "period": "24", "max_age": "1"})


def test_next_occurrence() -> None:
    assert next_occurrence("now", NOW, UTC_TZ) == NOW
    assert next_occurrence("03:00", NOW, UTC_TZ) == NOW + timedelta(hours=1)
    assert next_occurrence("01:00", NOW, UTC_TZ) == NOW + timedelta(hours=23)
    assert next_occurrence("02:00", NOW, UTC_TZ) == NOW + timedelta(hours=24)
    berlin = ZoneInfo("Europe/Berlin")  # UTC+2 in October
    assert next_occurrence("05:00", NOW, berlin) == NOW + timedelta(hours=1)
    with pytest.raises(ValueError, match="HH:MM"):
        next_occurrence("2am", NOW, UTC_TZ)


# ---- /folder ----------------------------------------------------------------------


async def test_folder_creates_with_defaults(h: Harness) -> None:
    reply = await h.bot.command("folder", ["Pics"])
    assert reply == "✅ Pics: every 24h, posts 24–48h old, first fetch within a minute"
    folder = await h.storage.find_folder("Pics")
    assert folder is not None
    assert folder.config == FolderConfig() and folder.next_run_at == NOW


async def test_folder_creates_with_pairs(h: Harness) -> None:
    reply = await h.bot.command("folder", ["Pics", "min_age=24", "period=6"])
    assert reply == "✅ Pics: every 6h, posts 24–30h old, first fetch within a minute"
    reply = await h.bot.command("folder", ["More", "Pics", "next=03:00"])
    assert reply == "✅ More Pics: every 24h, posts 24–48h old, first fetch at 2026-10-15 03:00"


async def test_folder_updates_and_reports_changes(h: Harness) -> None:
    reply = await h.bot.command("folder", ["Memes", "batch=20", "max_age=7", "period=6"])
    assert reply == "✅ Memes: batch 20, max_age 7d, period 6h"
    folder = await h.storage.find_folder("Memes")
    assert folder is not None
    assert (folder.config.batch_size, folder.config.period_h) == (20, 6)
    assert folder.next_run_at == NOW  # period change keeps the schedule


async def test_folder_shows_config(h: Harness) -> None:
    assert await h.bot.command("folder", ["Memes"]) == MEMES_LINE


async def test_folder_loose_name_updates_not_creates(h: Harness) -> None:
    assert await h.bot.command("folder", ["MEMES", "batch=3"]) == "✅ Memes: batch 3"
    assert [f.name for f in await h.storage.list_folders()] == ["Memes", "My News"]


async def test_folder_off(h: Harness) -> None:
    assert await h.bot.command("folder", ["my", "news", "off"]) == (
        "✅ My News removed; digest channel kept"
    )
    assert [f.name for f in await h.storage.list_folders()] == ["Memes"]
    assert await h.bot.command("folder", ["Nope", "off"]) == "unknown folder: Nope"


async def test_folder_rejects_bad_input(h: Harness) -> None:
    assert await h.bot.command("folder", []) == "❌ folder name required"
    assert await h.bot.command("folder", ["Memes", "batchsize=3"]) == "❌ unknown key: batchsize"
    reply = await h.bot.command("folder", ["Memes", "period=0"])
    assert reply.startswith("❌ period_h:")
    folder = await h.storage.find_folder("Memes")
    assert folder is not None and folder.config.period_h == 24


async def test_folder_target_sets_mapping(h: Harness) -> None:
    assert await h.bot.command("folder", ["Memes", "target=-100777"]) == "✅ Memes: target -100777"
    assert await h.storage.get_target("Memes") == -100777
    assert await h.bot.command("folder", ["Memes", "target=abc"]) == (
        "❌ target: expected a channel id, got abc"
    )


async def test_folder_target_rejects_channel_of_other_folder(h: Harness) -> None:
    await h.bot.command("folder", ["Memes", "target=-100777"])
    assert await h.bot.command("folder", ["My", "News", "target=-100777"]) == (
        "❌ channel -100777 is already the digest channel of Memes"
    )
    assert await h.storage.get_target("My News") is None
    # re-setting the same channel on the same folder is fine
    assert await h.bot.command("folder", ["Memes", "target=-100777"]) == "✅ Memes: target -100777"


async def test_folder_target_replacing_auto_channel_unpins_it(h: Harness) -> None:
    await h.bot.command("run", ["Memes"])  # creates and pins the digest channel
    old = await h.storage.get_target("Memes")
    assert old is not None and old in h.user.folders["Memes"]
    reply = await h.bot.command("folder", ["Memes", "target=-100777"])
    assert reply == "✅ Memes: target -100777 (old digest channel removed from the folder)"
    assert await h.storage.get_target("Memes") == -100777
    assert h.user.folder_unpins == [("Memes", old)]
    assert old not in h.user.folders["Memes"]


async def test_folder_target_rejects_channel_mapped_to_removed_folder(h: Harness) -> None:
    await h.bot.command("folder", ["Memes", "target=-100777"])
    await h.bot.command("folder", ["Memes", "off"])
    assert await h.bot.command("folder", ["My", "News", "target=-100777"]) == (
        "❌ channel -100777 is already the digest channel of Memes"
    )
    assert await h.storage.get_target("My News") is None


async def test_folder_next_reanchors_schedule(h: Harness) -> None:
    reply = await h.bot.command("folder", ["Memes", "next=03:00"])
    assert reply == "✅ Memes: next 2026-10-15 03:00"
    folder = await h.storage.find_folder("Memes")
    assert folder is not None and folder.next_run_at == NOW + timedelta(hours=1)
    assert await h.bot.command("folder", ["Memes", "next=2am"]) == (
        "❌ next: expected HH:MM or now, got 2am"
    )


async def test_folder_scoring_change_rescores_queue(h: Harness) -> None:
    await h.bot.command("run", ["Memes"])  # batch 2 of 3 sent; msg 3 (engagement 7) pending
    folder = await h.storage.find_folder("Memes")
    assert folder is not None
    assert [i.score for i in await h.storage.pending_items(folder.id)] == [7 / 200]
    assert await h.bot.command("folder", ["Memes", "scoring=raw"]) == "✅ Memes: scoring raw"
    assert [i.score for i in await h.storage.pending_items(folder.id)] == [7.0]


# ---- /run, /status, /folders, /reset ------------------------------------------------


async def test_run_one_folder(h: Harness) -> None:
    assert await h.bot.command("run", ["Memes"]) == "✅ Memes: +3 · 3 queued"


async def test_run_all_folders(h: Harness) -> None:
    assert await h.bot.command("run", []) == "✅ Memes: +3 · 3 queued\n✅ My News: +0 · 0 queued"


async def test_run_accepts_text_only_name(h: Harness) -> None:
    assert await h.bot.command("run", ["memes"]) == "✅ Memes: +3 · 3 queued"
    assert await h.bot.command("run", ["my", "news", "24", "0"]) == "✅ My News: +0 · 0 queued"


async def test_run_unknown_folder(h: Harness) -> None:
    assert await h.bot.command("run", ["Nope"]) == "unknown folder: Nope"


async def test_run_rejects_inverted_hours(h: Harness) -> None:
    assert await h.bot.command("run", ["Memes", "0", "24"]) == "from_h must be greater than to_h"


async def test_run_without_folders(storage: Storage, tmp_path: Path) -> None:
    h = Harness(storage, tmp_path)
    assert await h.bot.command("run", []) == "no folders configured; /folder <name> adds one"


async def test_status(h: Harness) -> None:
    assert await h.bot.command("status", []) == (
        "Memes: never fetched · queued 0 · no batch yet · next 2026-10-15 02:00\n"
        "My News: never fetched · queued 0 · no batch yet · next 2026-10-15 02:00"
    )
    await h.bot.command("run", ["Memes"])
    text = await h.bot.command("status", [])
    assert text.splitlines()[0] == (
        "Memes: fetched 2026-10-15 02:00 · queued 1 · batch 2026-10-15 02:00 · "
        "next 2026-10-15 02:00"
    )


async def test_status_shows_last_error_and_unread_note(h: Harness) -> None:
    folder = await h.storage.find_folder("Memes")
    assert folder is not None
    folder.last_error = "folder not found"
    folder.fetch_added = 4
    await h.storage.update_folder_state(folder)
    text = await h.bot.command("status", [])
    assert text.splitlines()[0] == (
        "Memes: never fetched (+4 new) · queued 0 · no batch yet · next 2026-10-15 02:00 · "
        "⚠️ folder not found"
    )


async def test_folders_lists_settings_and_channels(h: Harness) -> None:
    h.user.folder_titles = ["🔥 Memes", "My News"]
    assert await h.bot.command("folders", []) == (
        f"Telegram folders: 🔥 Memes · My News\n{MEMES_LINE}\n  Alpha (@a) · 2.9k\n{NEWS_LINE}"
    )


async def test_folders_hides_digest_channel_added_to_the_folder(h: Harness) -> None:
    await h.bot.command("run", ["Memes"])
    assert h.user.folder_pins and h.user.folder_pins[0][0] == "Memes"
    text = await h.bot.command("folders", [])
    assert text.count("  ") == 1  # only Alpha is listed under Memes


async def test_folders_reports_missing_folder(h: Harness) -> None:
    del h.user.folders["My News"]
    text = await h.bot.command("folders", [])
    assert text.splitlines()[-1] == "  ❌ folder not found"


async def test_folders_without_folders(storage: Storage, tmp_path: Path) -> None:
    h = Harness(storage, tmp_path)
    assert await h.bot.command("folders", []) == (
        "Telegram folders: Memes · My News\nno folders configured; /folder <name> adds one"
    )


async def test_reset(h: Harness) -> None:
    assert await h.bot.command("reset", []) == help_for("reset")
    assert await h.bot.command("reset", ["Memes"]) == "no digest channel mapping for Memes"
    await h.bot.command("run", ["Memes"])
    assert await h.bot.command("reset", ["memes"]) == (
        "reset Memes; old digest channel removed from the folder; next run creates a new one"
    )
    await h.bot.command("run", ["Memes"])
    h.user.fail_unpin = True
    text = await h.bot.command("reset", ["Memes"])
    assert text.startswith("reset Memes; ⚠️ old digest channel ")
    assert "still in the folder (FILTER_INCLUDE_EMPTY)" in text
    assert text.endswith("remove it by hand; next run creates a new one")


def test_help_overview_covers_every_command() -> None:
    names = ["folder", "folders", "run", "status", "skip", "unskip", "reset", "help"]
    assert [name for name, _ in menu()] == names
    assert all(re.fullmatch(r"[a-z0-9_]{1,32}", name) for name, _ in menu())
    assert all(0 < len(summary) <= 256 for _, summary in menu())
    assert all(f"/{name}" in USAGE for name in names)
    assert USAGE.endswith("/help <command> shows details, e.g. /help folder")


def test_help_for_command() -> None:
    folder = help_for("folder")
    assert folder is not None
    assert folder.startswith("/folder <name> [key=value ...]")
    assert "min_age" in folder and "/folder Memes min_age=24 period=6" in folder
    assert help_for("nope") is None


async def test_help_and_unknown_commands(h: Harness) -> None:
    assert await h.bot.command("help", []) == USAGE
    assert await h.bot.command("start", []) == USAGE
    assert await h.bot.command("help", ["skip"]) == help_for("skip")
    assert await h.bot.command("help", ["bogus"]) == "unknown command /bogus; /help lists them"
    assert await h.bot.command("bogus", []) == "unknown command /bogus; /help lists them"


async def test_skip_adds_lists_and_removes(h: Harness) -> None:
    assert await h.bot.command("skip", []) == "no skip phrases; /skip <phrase> adds one"
    assert await h.bot.command("skip", ["#Реклама"]) == "✅ skipping #реклама"
    assert await h.bot.command("skip", ["#реклама"]) == "already skipping #реклама"
    assert await h.bot.command("skip", ["текст", "прислан"]) == "✅ skipping текст прислан"
    assert await h.bot.command("skip", []) == "🚫 #реклама\n🚫 текст прислан"
    assert await h.bot.command("unskip", ["#реклама"]) == "✅ #реклама no longer skipped"
    assert await h.bot.command("unskip", ["#реклама"]) == "not skipping #реклама"
    assert await h.bot.command("unskip", []) == help_for("unskip")
