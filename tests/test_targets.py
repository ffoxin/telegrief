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
    resolver = TargetResolver(user, storage, "telegrief_bot")
    cfg = FolderConfig(name="Memes")
    first = await resolver.resolve(cfg, NOW)
    second = await resolver.resolve(cfg, NOW)
    assert first == second
    assert user.created == [("Memes · digest", "telegrief_bot")]
    assert await storage.get_target("Memes") == first


async def test_reset_clears_mapping(storage: Storage) -> None:
    user = FakeUser()
    resolver = TargetResolver(user, storage, "telegrief_bot")
    cfg = FolderConfig(name="Memes")
    first = await resolver.resolve(cfg, NOW)
    assert await resolver.reset("Memes") is True
    assert await resolver.reset("Memes") is False
    assert await resolver.resolve(cfg, NOW) != first
    assert len(user.created) == 2
