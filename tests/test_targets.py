from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegrief.config import FolderConfig
from telegrief.storage import Storage
from telegrief.targets import TargetResolver
from tests.fakes import FakeUser

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = await Storage.open(tmp_path / "t.db")
    yield s
    await s.close()


async def test_yaml_target_wins(storage: Storage) -> None:
    user = FakeUser()
    resolver = TargetResolver(user, storage, "telegrief_bot")
    assert await resolver.resolve(FolderConfig(name="Memes", target=-100777), NOW) == -100777
    assert user.created == []
    assert await storage.get_target("Memes") is None


async def test_creates_channel_once_and_stores_mapping(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = []
    resolver = TargetResolver(user, storage, "telegrief_bot")
    cfg = FolderConfig(name="Memes")
    first = await resolver.resolve(cfg, NOW)
    second = await resolver.resolve(cfg, NOW)
    assert first == second
    assert user.created == [("Memes · digest", "telegrief_bot")]
    assert await storage.get_target("Memes") == first


async def test_reset_clears_mapping(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = []
    resolver = TargetResolver(user, storage, "telegrief_bot")
    cfg = FolderConfig(name="Memes")
    first = await resolver.resolve(cfg, NOW)
    result = await resolver.reset("Memes")
    assert result is not None
    assert (result.channel_id, result.removed_from_folder, result.error) == (first, True, None)
    assert user.folder_unpins == [("Memes", first)]
    assert first not in user.folders["Memes"]
    assert await resolver.reset("Memes") is None
    assert await resolver.resolve(cfg, NOW) != first
    assert len(user.created) == 2


async def test_reset_reports_channel_not_in_folder(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = []
    resolver = TargetResolver(user, storage, "telegrief_bot")
    first = await resolver.resolve(FolderConfig(name="Memes"), NOW)
    user.folders["Memes"].remove(first)  # user already removed it by hand
    result = await resolver.reset("Memes")
    assert result is not None
    assert (result.removed_from_folder, result.error) == (False, None)


async def test_reset_keeps_mapping_deleted_when_folder_update_fails(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = []
    resolver = TargetResolver(user, storage, "telegrief_bot")
    first = await resolver.resolve(FolderConfig(name="Memes"), NOW)
    user.fail_unpin = True
    result = await resolver.reset("Memes")
    assert result is not None
    assert result.removed_from_folder is False
    assert result.error is not None and "FILTER_INCLUDE_EMPTY" in result.error
    assert await storage.get_target("Memes") is None
    assert first in user.folders["Memes"]


async def test_created_channel_named_after_telegram_folder_title_without_emoji(
    storage: Storage,
) -> None:
    user = FakeUser()
    user.folders["Memes"] = []
    user.real_titles["Memes"] = "😊mEmEs 🔥"
    resolver = TargetResolver(user, storage, "telegrief_bot")
    await resolver.resolve(FolderConfig(name="Memes"), NOW)
    assert user.created == [("mEmEs · digest", "telegrief_bot")]


async def test_created_channel_falls_back_to_yaml_name_for_emoji_only_title(
    storage: Storage,
) -> None:
    user = FakeUser()
    user.folders["Memes"] = []
    user.real_titles["Memes"] = "🔥🔥"
    resolver = TargetResolver(user, storage, "telegrief_bot")
    await resolver.resolve(FolderConfig(name="Memes"), NOW)
    assert user.created == [("Memes · digest", "telegrief_bot")]


async def test_resolve_pins_new_channel_in_its_folder(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = [-1001]
    resolver = TargetResolver(user, storage, "telegrief_bot")
    channel_id = await resolver.resolve(FolderConfig(name="Memes"), NOW)
    assert user.folder_pins == [("Memes", channel_id)]
    assert user.folders["Memes"] == [channel_id, -1001]


async def test_resolve_adds_existing_channel_to_folder_only_when_missing(
    storage: Storage,
) -> None:
    user = FakeUser()
    user.folders["Memes"] = []
    resolver = TargetResolver(user, storage, "telegrief_bot")
    cfg = FolderConfig(name="Memes")
    channel_id = await resolver.resolve(cfg, NOW)
    await resolver.resolve(cfg, NOW)
    assert user.folder_pins == [("Memes", channel_id)]  # already there: untouched
    user.folders["Memes"].remove(channel_id)  # user removed it, or mapping predates pinning
    await resolver.resolve(cfg, NOW)
    assert user.folder_pins == [("Memes", channel_id), ("Memes", channel_id)]


async def test_resolve_does_not_touch_folder_for_yaml_target(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = []
    resolver = TargetResolver(user, storage, "telegrief_bot")
    await resolver.resolve(FolderConfig(name="Memes", target=-100777), NOW)
    assert user.folder_pins == []


async def test_resolve_survives_folder_update_failure(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = []
    user.fail_pin = True
    resolver = TargetResolver(user, storage, "telegrief_bot")
    channel_id = await resolver.resolve(FolderConfig(name="Memes"), NOW)
    assert channel_id == await storage.get_target("Memes")
