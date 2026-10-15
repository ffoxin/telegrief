"""Resolve (or create) the digest channel for a folder."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from loguru import logger

from .config import FolderConfig
from .tg.protocols import UserGateway

if TYPE_CHECKING:
    from .storage import Storage


class TargetResolver:
    def __init__(self, user: UserGateway, storage: Storage, bot_username: str) -> None:
        self._user = user
        self._storage = storage
        self._bot_username = bot_username

    async def resolve(self, cfg: FolderConfig, now: datetime) -> int:
        if cfg.target is not None:
            return cfg.target
        existing = await self._storage.get_target(cfg.name)
        if existing is not None:
            return existing
        channel_id = await self._user.create_digest_channel(
            f"{cfg.name} · digest", self._bot_username
        )
        await self._storage.set_target(cfg.name, channel_id, now)
        logger.info("folder {}: created digest channel {}", cfg.name, channel_id)
        return channel_id

    async def reset(self, folder: str) -> bool:
        return await self._storage.delete_target(folder)
