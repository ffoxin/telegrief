"""Queue lifecycle: fetch into the queue, send batches, auto-next on read, manual next."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

from .collector import Collector, digest_targets, drop_skipped
from .config import Settings
from .delivery import Deliverer, render_fetch_note, render_footer
from .models import ChannelInfo, Folder, ItemState, QueueItem
from .scoring import rank, score
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
    """Per-folder queue: fetches add posts, batches drain the best ones into the digest channel.

    One lock per folder serialises fetch, send_batch and rescore. ``fetch`` sends the first
    batch from inside its lock through ``_send_batch``; everything else goes through
    ``send_batch``.
    """

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
        self._clock = clock
        self._defer = defer
        self._on_fatal = on_fatal
        self._locks: dict[int, asyncio.Lock] = {}
        self._rechecks: set[int] = set()

    def _lock(self, folder_id: int) -> asyncio.Lock:
        return self._locks.setdefault(folder_id, asyncio.Lock())

    def _fatal(self, exc: Exception) -> None:
        logger.critical("user session is dead: {}", exc)
        if self._on_fatal is not None:
            self._on_fatal(exc)

    def bind(self) -> None:
        self._user.on_channel_read(self.on_read)
        self._bot.on_next_button(self.on_next_button)

    async def _name(self, folder_id: int) -> str:
        folder = await self._storage.get_folder(folder_id)
        return folder.name if folder is not None else f"folder {folder_id}"

    # ---- fetch ---------------------------------------------------------------

    async def fetch_all(self, from_h: int | None = None, to_h: int | None = None) -> list[str]:
        return [
            await self.fetch(f.id, from_h=from_h, to_h=to_h)
            for f in await self._storage.list_folders()
        ]

    async def fetch(
        self,
        folder_id: int,
        *,
        at: datetime | None = None,
        from_h: int | None = None,
        to_h: int | None = None,
        scheduled: bool = False,
    ) -> str:
        """Collect one window into the folder's queue; post the note; maybe send a batch.

        ``at`` is the reference time of the window (the slot time for scheduled runs,
        now otherwise); ``from_h`` / ``to_h`` override the bounds, in hours before now.
        Returns a one-line result for the owner.
        """
        async with self._lock(folder_id):
            return await self._fetch(folder_id, at, from_h, to_h, scheduled)

    async def _fetch(
        self,
        folder_id: int,
        at: datetime | None,
        from_h: int | None,
        to_h: int | None,
        scheduled: bool,
    ) -> str:
        folder = await self._storage.get_folder(folder_id)
        if folder is None:
            return f"❌ unknown folder id {folder_id}"
        cfg = folder.config
        now = self._clock()
        start, end = cfg.window(at if at is not None else now)
        if from_h is not None:
            start = now - timedelta(hours=from_h)
        if to_h is not None:
            end = now - timedelta(hours=to_h)
        if start >= end:
            return f"❌ {folder.name}: empty window"
        try:
            target = await self._targets.resolve(folder.name, now)
            exclude = await digest_targets(self._storage)
            collected = await self._collector.collect(folder.name, start, end, now, exclude=exclude)
            kept = drop_skipped(collected.posts, await self._storage.skip_phrases())
            skipped = len(collected.posts) - len(kept)
            scored = rank(kept, collected.channels, cfg)
            added = await self._storage.enqueue(folder.id, scored, now)
            await self._storage.prune(folder.id, cfg, now)
            pending = await self._storage.count_pending(folder.id)
            folder.last_fetch_at = now
            folder.last_error = None
            await self._storage.update_folder_state(folder)
            await self._post_note(folder, target, added, pending)
            await self._kick(folder, target, now)
        except SessionDead as exc:
            message = f"💀 {folder.name}: user session dead, run `telegrief login`: {exc}"
            await self._bot.send_owner(message)
            self._fatal(exc)
            return message
        except Exception as exc:
            logger.exception("folder {}: fetch failed", folder.name)
            message = f"❌ {folder.name}: {exc}"
            await self._report_failure(folder.id, str(exc), message, scheduled)
            return message
        message = f"✅ {folder.name}: +{added} · {pending} queued"
        if skipped:
            message += f" · 🚫 {skipped} skipped"
        if collected.errors:
            message += f" · ⚠️ skipped: {', '.join(collected.errors)}"
        logger.info(message)
        return message

    async def _report_failure(
        self, folder_id: int, error: str, message: str, scheduled: bool
    ) -> None:
        """Scheduled runs DM the owner once per distinct error; manual runs only reply."""
        if not scheduled:
            return
        folder = await self._storage.get_folder(folder_id)
        if folder is None:
            return
        if error != folder.last_error:
            await self._bot.send_owner(message)
        folder.last_error = error
        await self._storage.update_folder_state(folder)

    async def _post_note(self, folder: Folder, target: int, added: int, pending: int) -> None:
        """``📥 +N · M queued``: edited in place until a batch goes out, then a new one."""
        if folder.fetch_msg_id is None and added == 0:
            return
        folder.fetch_added += added
        text = render_fetch_note(folder.fetch_added, pending)
        next_id = folder.id if pending else None
        label = f"Next {folder.config.batch_size}"
        if folder.fetch_msg_id is not None:
            try:
                await self._bot.edit_text(
                    target, folder.fetch_msg_id, text, next_id=next_id, next_label=label
                )
            except TargetUnavailable:
                raise
            except GatewayError as exc:
                logger.warning(
                    "folder {}: cannot edit fetch note {}: {}",
                    folder.name,
                    folder.fetch_msg_id,
                    exc,
                )
            else:
                self._hold_button(folder, folder.fetch_msg_id, text, next_id)
                await self._storage.update_folder_state(folder)
                return
        await self._drop_button(folder, target)
        folder.fetch_msg_id = await self._bot.send_text(
            target, text, next_id=next_id, next_label=label
        )
        self._hold_button(folder, folder.fetch_msg_id, text, next_id)
        await self._storage.update_folder_state(folder)

    @staticmethod
    def _hold_button(folder: Folder, msg_id: int, text: str, next_id: int | None) -> None:
        folder.button_msg_id = msg_id if next_id is not None else None
        folder.button_text = text if next_id is not None else None

    async def _drop_button(self, folder: Folder, target: int) -> None:
        """Edit the current button holder (note or footer) so it loses its button."""
        if folder.button_msg_id is None or folder.button_text is None:
            return
        try:
            await self._bot.edit_text(target, folder.button_msg_id, folder.button_text)
        except TargetUnavailable:
            raise
        except GatewayError as exc:
            logger.warning(
                "folder {}: cannot drop button from {}: {}",
                folder.name,
                folder.button_msg_id,
                exc,
            )
        folder.button_msg_id = None
        folder.button_text = None

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

    def _too_soon(self, folder: Folder, now: datetime) -> bool:
        if folder.last_batch_at is None:
            return False
        elapsed = (now - folder.last_batch_at).total_seconds()
        return elapsed < self._settings.auto_next_min_interval_s

    async def send_batch(self, folder_id: int, *, manual: bool = False) -> int:
        """Deliver the next batch of pending items. Returns how many were attempted.

        Automatic sends (read marker) re-check pacing under the lock, so two
        triggers that both passed the pre-check collapse into one batch.
        """
        async with self._lock(folder_id):
            return await self._send_batch(folder_id, manual=manual)

    async def _send_batch(self, folder_id: int, *, manual: bool) -> int:
        folder = await self._storage.get_folder(folder_id)
        if folder is None:
            return 0
        target = await self._storage.get_target(folder.name)
        if target is None:
            return 0
        cfg = folder.config
        now = self._clock()
        if not manual and self._too_soon(folder, now):
            return 0
        await self._storage.prune(folder.id, cfg, now)
        items = await self._storage.pending_items(folder.id, cfg.batch_size)
        if not items:
            return 0
        channels: dict[int, ChannelInfo] = {}
        for item in items:
            channel = channels.get(item.channel_id)
            if channel is None:
                channel = channels[item.channel_id] = await self._channel(item.channel_id, now)
            try:
                kind, last_id = await self._deliverer.deliver(target, item, channel)
            except (TargetUnavailable, SessionDead):
                raise
            except Exception:
                logger.exception(
                    "folder {}: {}/{} failed", folder.name, item.channel_id, item.msg_id
                )
                item.state = ItemState.FAILED
            else:
                item.state = ItemState.SENT
                item.delivery = kind
                item.sent_msg_id = last_id
                item.sent_at = now
            await self._storage.update_item(item)
        pending = await self._storage.count_pending(folder.id)
        sent = [i for i in items if i.state == ItemState.SENT]
        text = render_footer(sent, channels, cfg, pending)
        await self._drop_button(folder, target)
        next_id = folder.id if pending else None
        msg_id = await self._bot.send_text(
            target, text, next_id=next_id, next_label=f"Next {cfg.batch_size}"
        )
        self._hold_button(folder, msg_id, text, next_id)
        folder.fetch_msg_id = None
        folder.fetch_added = 0
        folder.last_batch_at = now
        await self._storage.update_folder_state(folder)
        return len(items)

    def _batch_read(self, batch: list[QueueItem], max_id: int) -> bool:
        """True once the read marker covers the configured share of the batch's posts."""
        seen = sum(1 for i in batch if i.sent_msg_id is not None and i.sent_msg_id <= max_id)
        needed = math.ceil(round(self._settings.auto_next_read_fraction * len(batch), 6))
        return seen >= max(1, needed)

    async def _next_due(self, folder: Folder, max_id: int, now: datetime) -> float | None:
        """Seconds until the next batch may go out: 0 for now, None when nothing is due."""
        if await self._storage.count_pending(folder.id) == 0:
            return None
        if folder.last_batch_at is None:
            return 0.0
        batch = await self._storage.batch_items(folder.id, folder.last_batch_at)
        if not self._batch_read(batch, max_id):
            return None
        elapsed = (now - folder.last_batch_at).total_seconds()
        return max(0.0, self._settings.auto_next_min_interval_s - elapsed)

    async def _kick(self, folder: Folder, target: int, now: datetime) -> None:
        """After a fetch: send a batch if the channel is idle (read), else wait for reads."""
        marker = await self._user.read_marker(target) if folder.last_batch_at is not None else 0
        wait = await self._next_due(folder, marker, now)
        if wait is None:
            return
        if wait <= 0:
            await self._send_batch(folder.id, manual=False)
            return
        self._schedule_recheck(folder.id, target, wait)

    def _schedule_recheck(self, folder_id: int, target: int, wait: float) -> None:
        if folder_id in self._rechecks:
            return
        self._rechecks.add(folder_id)

        async def recheck() -> None:
            self._rechecks.discard(folder_id)
            try:
                marker = await self._user.read_marker(target)
                await self.on_read(target, marker)
            except Exception:
                logger.exception("folder {}: deferred recheck failed", folder_id)

        self._defer(wait, recheck)

    async def _guarded_send(self, folder_id: int, *, manual: bool) -> None:
        try:
            await self.send_batch(folder_id, manual=manual)
        except SessionDead as exc:
            name = await self._name(folder_id)
            await self._bot.send_owner(f"💀 {name}: user session dead: {exc}")
            self._fatal(exc)
        except TargetUnavailable as exc:
            name = await self._name(folder_id)
            logger.error("folder {}: digest channel unusable: {}", name, exc)
            await self._bot.send_owner(f"❌ {name}: digest channel unusable: {exc}")
        except Exception as exc:
            name = await self._name(folder_id)
            logger.exception("folder {}: batch failed", name)
            await self._bot.send_owner(f"❌ {name}: {exc}")

    # ---- triggers ------------------------------------------------------------

    async def on_read(self, channel_id: int, max_id: int) -> None:
        folder = await self._storage.folder_for_target(channel_id)
        if folder is None:
            return
        wait = await self._next_due(folder, max_id, self._clock())
        if wait is None:
            return
        if wait <= 0:
            await self._guarded_send(folder.id, manual=False)
            return
        self._schedule_recheck(folder.id, channel_id, wait)

    async def on_next_button(self, folder_id: int) -> None:
        await self._guarded_send(folder_id, manual=True)

    async def catch_up(self) -> None:
        """After a restart: a batch read while we were down still triggers the next one."""
        for folder in await self._storage.list_folders():
            target = await self._storage.get_target(folder.name)
            if target is None:
                continue
            try:
                marker = await self._user.read_marker(target)
            except SessionDead as exc:
                self._fatal(exc)
                return
            except Exception as exc:
                logger.exception("folder {}: cannot read marker", folder.name)
                await self._bot.send_owner(f"❌ {folder.name}: digest channel unusable: {exc}")
                continue
            await self.on_read(target, marker)

    # ---- config changes ------------------------------------------------------

    async def rescore(self, folder_id: int) -> int:
        """Re-score pending items with the folder's current strategy. Returns the count."""
        async with self._lock(folder_id):
            folder = await self._storage.get_folder(folder_id)
            if folder is None:
                return 0
            scores: list[tuple[int, int, float]] = []
            for item in await self._storage.pending_items(folder.id):
                cached = await self._storage.get_channel(item.channel_id)
                subscribers = cached[0].subscribers if cached is not None else 0
                scores.append(
                    (
                        item.channel_id,
                        item.msg_id,
                        score(item.engagement, item.views, subscribers, folder.config),
                    )
                )
            await self._storage.update_scores(folder.id, scores)
            return len(scores)
