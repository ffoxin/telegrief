"""Resolve (or create) the digest channel for a folder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from loguru import logger

from .config import FolderConfig, strip_symbols
from .tg.protocols import GatewayError, SessionDead, UserGateway

if TYPE_CHECKING:
    from .storage import Storage


@dataclass(frozen=True)
class ResetResult:
    channel_id: int
    removed_from_folder: bool
    error: str | None = None


class TargetResolver:
    def __init__(self, user: UserGateway, storage: Storage, bot_username: str) -> None:
        self._user = user
        self._storage = storage
        self._bot_username = bot_username

    async def resolve(self, cfg: FolderConfig, now: datetime) -> int:
        if cfg.target is not None:
            return cfg.target
        channel_id = await self._storage.get_target(cfg.name)
        if channel_id is None:
            # Channel titles cannot hold custom emoji, so drop emoji altogether.
            title = strip_symbols(await self._user.folder_title(cfg.name)) or cfg.name
            channel_id = await self._user.create_digest_channel(
                f"{title} · digest", self._bot_username
            )
            await self._storage.set_target(cfg.name, channel_id, now)
            logger.info("folder {}: created digest channel {}", cfg.name, channel_id)
        await self._pin_in_folder(cfg.name, channel_id)
        return channel_id

    async def _pin_in_folder(self, folder: str, channel_id: int) -> None:
        """Best effort: put the digest channel at the top of its source folder."""
        try:
            if await self._user.ensure_in_folder(folder, channel_id):
                logger.info("folder {}: pinned digest channel {} in the folder", folder, channel_id)
        except SessionDead:
            raise
        except GatewayError as exc:
            logger.warning("folder {}: cannot add digest channel to the folder: {}", folder, exc)

    async def reset(self, folder: str) -> ResetResult | None:
        """Forget the digest channel and take it out of the folder so it is not a source."""
        channel_id = await self._storage.get_target(folder)
        if channel_id is None:
            return None
        await self._storage.delete_target(folder)
        try:
            removed = await self._user.remove_from_folder(folder, channel_id)
        except SessionDead:
            raise
        except GatewayError as exc:
            logger.warning(
                "folder {}: cannot remove old digest channel {}: {}", folder, channel_id, exc
            )
            return ResetResult(channel_id, False, str(exc))
        return ResetResult(channel_id, removed)
