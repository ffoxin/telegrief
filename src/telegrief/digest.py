"""Digest lifecycle: build, send batches, auto-next on read, manual next."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from loguru import logger

from .collector import Collector, window
from .config import FolderConfig, FoldersConfig, Settings
from .delivery import Deliverer, render_footer, render_title
from .models import ChannelInfo, Digest, DigestStatus, ItemState
from .scoring import rank
from .targets import TargetResolver
from .tg.protocols import BotGateway, ChannelUnavailable, GatewayError, UserGateway

if TYPE_CHECKING:
    from .storage import Storage

Deferrer = Callable[[float, Callable[[], Awaitable[None]]], None]


def _asyncio_defer(delay: float, callback: Callable[[], Awaitable[None]]) -> None:
    loop = asyncio.get_running_loop()
    loop.call_later(delay, lambda: asyncio.ensure_future(callback()))


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DigestService:
    def __init__(
        self,
        *,
        user: UserGateway,
        bot: BotGateway,
        storage: Storage,
        collector: Collector,
        targets: TargetResolver,
        deliverer: Deliverer,
        settings: Settings,
        folders: Callable[[], FoldersConfig],
        clock: Callable[[], datetime] = _utcnow,
        defer: Deferrer = _asyncio_defer,
    ) -> None:
        self._user = user
        self._bot = bot
        self._storage = storage
        self._collector = collector
        self._targets = targets
        self._deliverer = deliverer
        self._settings = settings
        self._folders = folders
        self._clock = clock
        self._defer = defer
        self._locks: dict[int, asyncio.Lock] = {}
        self._rechecks: set[int] = set()

    def bind(self) -> None:
        self._user.on_channel_read(self.on_read)
        self._bot.on_next_button(self.on_next_button)

    # ---- runs ----------------------------------------------------------------

    async def run_all(self, from_h: int | None = None, to_h: int | None = None) -> list[str]:
        return [await self.run_folder(cfg, from_h, to_h) for cfg in self._folders().folders]

    async def run_folder(
        self, cfg: FolderConfig, from_h: int | None = None, to_h: int | None = None
    ) -> str:
        now = self._clock()
        start, end = window(
            now,
            from_h if from_h is not None else self._settings.window_from_h,
            to_h if to_h is not None else self._settings.window_to_h,
        )
        try:
            digest, errors = await self.build_digest(cfg, now, start, end)
        except Exception as exc:
            logger.exception("folder {}: run failed", cfg.name)
            message = f"❌ {cfg.name}: {exc}"
            await self._bot.send_owner(message)
            return message
        message = f"✅ {cfg.name}: {digest.total} posts"
        if errors:
            message += f" · ⚠️ skipped: {', '.join(errors)}"
        logger.info(message)
        return message

    async def build_digest(
        self, cfg: FolderConfig, now: datetime, start: datetime, end: datetime
    ) -> tuple[Digest, list[str]]:
        target = await self._targets.resolve(cfg, now)
        collected = await self._collector.collect(cfg.name, start, end, now)
        scored = rank(collected.posts, collected.channels, cfg)
        digest = await self._storage.create_digest(cfg.name, target, start, end, scored, now)
        day = end.astimezone(ZoneInfo(self._settings.tz)).date()
        title = render_title(cfg.name, day, digest.total, len({s.channel.id for s in scored}))
        try:
            digest.title_msg_id = await self._bot.send_text(target, title)
        except GatewayError:
            digest.status = DigestStatus.FAILED
            await self._storage.update_digest(digest)
            raise
        if digest.total == 0:
            digest.status = DigestStatus.DONE
            await self._storage.update_digest(digest)
            return digest, collected.errors
        await self._storage.update_digest(digest)
        try:
            await self.send_batch(digest.id)
        except GatewayError:
            digest.status = DigestStatus.FAILED
            await self._storage.update_digest(digest)
            raise
        return digest, collected.errors

    # ---- batches -------------------------------------------------------------

    async def _channel(self, channel_id: int, now: datetime) -> ChannelInfo:
        try:
            return await self._collector.channel_info(channel_id, now)
        except ChannelUnavailable:
            return ChannelInfo(
                id=channel_id,
                username=None,
                title=str(channel_id),
                subscribers=0,
                reactions_enabled=True,
                noforwards=False,
            )

    async def send_batch(self, digest_id: int) -> int:
        """Deliver the next batch of pending items. Returns how many were attempted."""
        lock = self._locks.setdefault(digest_id, asyncio.Lock())
        async with lock:
            digest = await self._storage.get_digest(digest_id)
            if digest is None or digest.status != DigestStatus.OPEN:
                return 0
            cfg = self._folders().get(digest.folder)
            if cfg is None:
                logger.warning(
                    "digest {}: folder {} no longer configured", digest_id, digest.folder
                )
                return 0
            now = self._clock()
            items = await self._storage.pending_items(digest.id, cfg.batch_size)
            channels: dict[int, ChannelInfo] = {}
            for item in items:
                channel = channels.get(item.channel_id)
                if channel is None:
                    channel = channels[item.channel_id] = await self._channel(item.channel_id, now)
                try:
                    kind, last_id = await self._deliverer.deliver(digest.target, item, channel)
                except GatewayError:
                    raise
                except Exception:
                    logger.exception("digest {}: item #{} failed", digest.id, item.rank)
                    item.state = ItemState.FAILED
                else:
                    item.state = ItemState.SENT
                    item.delivery = kind
                    item.sent_msg_id = last_id
                    item.sent_at = now
                await self._storage.update_item(item)
            shown = await self._storage.count_items(digest.id, ItemState.SENT)
            pending = await self._storage.count_items(digest.id, ItemState.PENDING)
            done = pending == 0
            text = render_footer(
                [i for i in items if i.state == ItemState.SENT],
                channels,
                cfg,
                shown,
                digest.total,
                done,
            )
            if digest.footer_msg_id is not None and digest.footer_text is not None:
                try:
                    await self._bot.edit_text(
                        digest.target, digest.footer_msg_id, digest.footer_text
                    )
                except GatewayError as exc:
                    logger.warning("digest {}: could not edit old footer: {}", digest.id, exc)
            digest.footer_msg_id = await self._bot.send_text(
                digest.target,
                text,
                next_digest_id=None if done else digest.id,
                next_label=f"Next {cfg.batch_size}",
            )
            digest.footer_text = text
            digest.last_batch_at = now
            if done:
                digest.status = DigestStatus.DONE
            await self._storage.update_digest(digest)
            return len(items)

    async def _guarded_send(self, digest_id: int) -> None:
        try:
            await self.send_batch(digest_id)
        except Exception as exc:
            logger.exception("digest {}: batch failed", digest_id)
            await self._bot.send_owner(f"❌ digest {digest_id}: {exc}")

    # ---- triggers ------------------------------------------------------------

    async def on_read(self, channel_id: int, max_id: int) -> None:
        digest = await self._storage.open_digest_for_target(channel_id)
        if digest is None:
            return
        last = await self._storage.max_sent_msg_id(digest.id)
        if last is None or max_id < last:
            return
        if await self._storage.count_items(digest.id, ItemState.PENDING) == 0:
            return
        interval = float(self._settings.auto_next_min_interval_s)
        elapsed = (
            (self._clock() - digest.last_batch_at).total_seconds()
            if digest.last_batch_at is not None
            else interval
        )
        if elapsed >= interval:
            await self._guarded_send(digest.id)
            return
        if digest.id in self._rechecks:
            return
        self._rechecks.add(digest.id)
        target = digest.target
        digest_id = digest.id

        async def recheck() -> None:
            self._rechecks.discard(digest_id)
            marker = await self._user.read_marker(target)
            await self.on_read(target, marker)

        self._defer(interval - elapsed, recheck)

    async def on_next_button(self, digest_id: int) -> None:
        await self._guarded_send(digest_id)

    async def catch_up(self) -> None:
        for digest in await self._storage.open_digests():
            marker = await self._user.read_marker(digest.target)
            await self.on_read(digest.target, marker)
