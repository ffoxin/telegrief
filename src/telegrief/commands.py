"""Owner-only bot commands: /run, /status, /folders, /reset."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from .collector import Collector
from .config import FoldersConfig
from .digest import DigestService
from .models import ItemState
from .scoring import format_count
from .targets import TargetResolver
from .tg.protocols import FolderNotFound, UserGateway

if TYPE_CHECKING:
    from .storage import Storage

USAGE = (
    "/run [folder] [from_h] [to_h] — run now (defaults: 48 24)\n"
    "/status — open digests\n"
    "/folders — channels resolved per folder\n"
    "/reset <folder> — forget digest channel mapping"
)


def parse_run_args(args: list[str]) -> tuple[str | None, int | None, int | None]:
    """Trailing integers (up to two) are hours; the rest, joined, is the folder."""
    words = list(args)
    hours: list[int] = []
    while words and len(hours) < 2 and words[-1].lstrip("-").isdigit():
        hours.insert(0, int(words.pop()))
    folder = " ".join(words) or None
    from_h = hours[0] if hours else None
    to_h = hours[1] if len(hours) > 1 else None
    return folder, from_h, to_h


class CommandHandler:
    def __init__(
        self,
        service: DigestService,
        user: UserGateway,
        storage: Storage,
        collector: Collector,
        targets: TargetResolver,
        folders: Callable[[], FoldersConfig],
        clock: Callable[[], datetime],
    ) -> None:
        self._service = service
        self._user = user
        self._storage = storage
        self._collector = collector
        self._targets = targets
        self._folders = folders
        self._clock = clock

    async def handle(self, command: str, args: list[str]) -> str:
        if command == "run":
            return await self._run(args)
        if command == "status":
            return await self._status()
        if command == "folders":
            return await self._list_folders()
        if command == "reset":
            return await self._reset(args)
        return USAGE

    async def _run(self, args: list[str]) -> str:
        folder, from_h, to_h = parse_run_args(args)
        if from_h is not None and to_h is not None and from_h <= to_h:
            return "from_h must be greater than to_h"
        if folder is None:
            return "\n".join(await self._service.run_all(from_h, to_h))
        cfg = self._folders().get(folder)
        if cfg is None:
            return f"unknown folder: {folder}"
        return await self._service.run_folder(cfg, from_h, to_h)

    async def _status(self) -> str:
        lines: list[str] = []
        for cfg in self._folders().folders:
            digest = await self._storage.latest_digest(cfg.name)
            if digest is None:
                lines.append(f"{cfg.name}: none")
                continue
            sent = await self._storage.count_items(digest.id, ItemState.SENT)
            pending = await self._storage.count_items(digest.id, ItemState.PENDING)
            lines.append(
                f"{cfg.name}: {digest.window_end:%Y-%m-%d} · {digest.status} · "
                f"shown {sent}/{digest.total} · pending {pending}"
            )
        return "\n".join(lines)

    async def _list_folders(self) -> str:
        now = self._clock()
        lines: list[str] = []
        for cfg in self._folders().folders:
            try:
                channel_ids = await self._user.folder_channels(cfg.name)
            except FolderNotFound:
                lines.append(f"{cfg.name}: ❌ folder not found")
                continue
            lines.append(f"{cfg.name} ({len(channel_ids)}):")
            for channel_id in channel_ids:
                try:
                    info = await self._collector.channel_info(channel_id, now)
                except Exception as exc:
                    lines.append(f"  {channel_id} · ❌ {exc}")
                    continue
                lines.append(f"  {info.title} ({info.display}) · {format_count(info.subscribers)}")
        return "\n".join(lines)

    async def _reset(self, args: list[str]) -> str:
        folder = " ".join(args)
        if not folder:
            return USAGE
        if await self._targets.reset(folder):
            return f"reset {folder}; next run creates a new channel"
        return f"no digest channel mapping for {folder}"
