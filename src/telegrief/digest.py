"""Digest lifecycle: build, send batches, auto-next on read, manual next."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from loguru import logger

from .collector import Collector, window
from .config import FolderConfig, FoldersConfig, Settings
from .delivery import Deliverer, render_footer, render_title
from .models import ChannelInfo, Digest, DigestItem, DigestStatus, ItemState
from .scoring import rank
from .targets import TargetResolver
from .tg.protocols import (
    BotGateway,
    ChannelUnavailable,
    GatewayError,
    SessionDead,
    TargetUnavailable,
    UserGateway,
)

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
        on_fatal: Callable[[Exception], None] | None = None,
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
        self._on_fatal = on_fatal
        self._locks: dict[int, asyncio.Lock] = {}
        self._run_locks: dict[str, asyncio.Lock] = {}
        self._rechecks: set[int] = set()

    def _fatal(self, exc: Exception) -> None:
        logger.critical("user session is dead: {}", exc)
        if self._on_fatal is not None:
            self._on_fatal(exc)

    def bind(self) -> None:
        self._user.on_channel_read(self.on_read)
        self._bot.on_next_button(self.on_next_button)

    # ---- runs ----------------------------------------------------------------

    async def run_all(self, from_h: int | None = None, to_h: int | None = None) -> list[str]:
        return [await self.run_folder(cfg, from_h, to_h) for cfg in self._folders().folders]

    async def run_folder(
        self, cfg: FolderConfig, from_h: int | None = None, to_h: int | None = None
    ) -> str:
        lock = self._run_locks.setdefault(cfg.name, asyncio.Lock())
        async with lock:
            return await self._run_folder(cfg, from_h, to_h)

    async def _run_folder(self, cfg: FolderConfig, from_h: int | None, to_h: int | None) -> str:
        now = self._clock()
        start, end = window(
            now,
            from_h if from_h is not None else self._settings.window_from_h,
            to_h if to_h is not None else self._settings.window_to_h,
        )
        try:
            digest, errors = await self.build_digest(cfg, now, start, end)
        except SessionDead as exc:
            message = f"💀 {cfg.name}: user session dead, run `telegrief login`: {exc}"
            await self._bot.send_owner(message)
            self._fatal(exc)
            return message
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
            if digest.total == 0:
                digest.status = DigestStatus.DONE
                await self._storage.update_digest(digest)
                return digest, collected.errors
            await self._storage.update_digest(digest)
            await self.send_batch(digest.id, manual=True)
        except Exception:
            await self._mark_failed(digest.id)
            raise
        return digest, collected.errors

    async def _mark_failed(self, digest_id: int) -> None:
        digest = await self._storage.get_digest(digest_id)
        if digest is not None and digest.status == DigestStatus.OPEN:
            digest.status = DigestStatus.FAILED
            await self._storage.update_digest(digest)

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

    def _too_soon(self, digest: Digest, now: datetime) -> bool:
        if digest.last_batch_at is None:
            return False
        elapsed = (now - digest.last_batch_at).total_seconds()
        return elapsed < self._settings.auto_next_min_interval_s

    async def send_batch(self, digest_id: int, *, manual: bool = False) -> int:
        """Deliver the next batch of pending items. Returns how many were attempted.

        Automatic sends (read marker) re-check pacing under the lock, so two
        triggers that both passed the pre-check collapse into one batch.
        """
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
            if not manual and self._too_soon(digest, now):
                return 0
            items = await self._storage.pending_items(digest.id, cfg.batch_size)
            channels: dict[int, ChannelInfo] = {}
            for item in items:
                channel = channels.get(item.channel_id)
                if channel is None:
                    channel = channels[item.channel_id] = await self._channel(item.channel_id, now)
                try:
                    kind, last_id = await self._deliverer.deliver(digest.target, item, channel)
                except (TargetUnavailable, SessionDead):
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

    def _batch_read(self, batch: list[DigestItem], max_id: int) -> bool:
        """True once the read marker covers the configured share of the batch's posts."""
        seen = sum(1 for i in batch if i.sent_msg_id is not None and i.sent_msg_id <= max_id)
        needed = math.ceil(round(self._settings.auto_next_read_fraction * len(batch), 6))
        return seen >= max(1, needed)

    async def _guarded_send(self, digest_id: int, *, manual: bool) -> None:
        try:
            await self.send_batch(digest_id, manual=manual)
        except SessionDead as exc:
            await self._bot.send_owner(f"💀 digest {digest_id}: user session dead: {exc}")
            self._fatal(exc)
        except TargetUnavailable as exc:
            logger.error("digest {}: target unusable: {}", digest_id, exc)
            await self._mark_failed(digest_id)
            await self._bot.send_owner(
                f"❌ digest {digest_id}: channel unusable, marked failed: {exc}"
            )
        except Exception as exc:
            logger.exception("digest {}: batch failed", digest_id)
            await self._bot.send_owner(f"❌ digest {digest_id}: {exc}")

    # ---- triggers ------------------------------------------------------------

    async def on_read(self, channel_id: int, max_id: int) -> None:
        digest = await self._storage.open_digest_for_target(channel_id)
        if digest is None:
            return
        if digest.last_batch_at is None:
            return
        batch = await self._storage.batch_items(digest.id, digest.last_batch_at)
        if not batch or not self._batch_read(batch, max_id):
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
            await self._guarded_send(digest.id, manual=False)
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
        await self._guarded_send(digest_id, manual=True)

    async def catch_up(self) -> None:
        """After a restart: a batch read while we were down still triggers the next."""
        for digest in await self._storage.open_digests():
            try:
                marker = await self._user.read_marker(digest.target)
            except SessionDead as exc:
                self._fatal(exc)
                return
            except Exception as exc:
                logger.exception("digest {}: cannot read marker, marking failed", digest.id)
                await self._mark_failed(digest.id)
                await self._bot.send_owner(f"❌ digest {digest.id}: channel unusable: {exc}")
                continue
            await self.on_read(digest.target, marker)
