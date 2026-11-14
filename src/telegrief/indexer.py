"""Search index of delivered posts: their text, what their pictures show, what is said."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from loguru import logger

from .errors import Busy
from .models import IndexEntry, QueueItem
from .speech import Transcriber, Untranscribable
from .tg.protocols import SessionDead, UserGateway
from .vision import Describer, Undescribable

if TYPE_CHECKING:
    from .storage import Storage

MAX_ATTEMPTS = 4
RETRY_S = 600.0


def fold(text: str) -> str:
    """Comparison form for matching: case and ё/е folded, whitespace collapsed."""
    return " ".join(text.casefold().replace("ё", "е").split())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Indexer:
    """Indexes posts in the background, after delivery, so batches are never held up.

    A failed entry is retried every ``retry_s``; the last of ``MAX_ATTEMPTS`` leaves the
    media out, so a post whose pictures or sound cannot be read is still found by its text.
    A rate limit is not a failure: the pass stops and the entry waits, untouched.
    """

    def __init__(
        self,
        user: UserGateway,
        storage: Storage,
        describer: Describer | None,
        transcriber: Transcriber | None = None,
        *,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        retry_s: float = RETRY_S,
    ) -> None:
        self._user = user
        self._storage = storage
        self._describer = describer
        self._transcriber = transcriber
        self._clock = clock
        self._monotonic = monotonic
        self._retry_s = retry_s
        self._retry_at: dict[int, float] = {}
        self._wake = asyncio.Event()

    async def add(self, folder: str, target: int, item: QueueItem) -> bool:
        added = await self._storage.index_add(folder, target, item, self._clock())
        self.wake()
        return added

    def wake(self) -> None:
        self._wake.set()

    async def drain(self) -> int:
        """Index every pending entry that is due. Returns how many got indexed."""
        done = 0
        for entry in await self._storage.index_pending():
            if self._retry_at.get(entry.id, 0.0) > self._monotonic():
                continue
            try:
                done += await self._index(entry)
            except Busy as exc:
                logger.info("indexing paused, {} posts done this pass: {}", done, exc)
                break
        return done

    async def _index(self, entry: IndexEntry) -> bool:
        last = entry.attempts >= MAX_ATTEMPTS - 1
        describer = None if last else self._describer
        transcriber = None if last else self._transcriber
        try:
            content = await self._user.post_content(
                entry.channel_id,
                list(entry.msg_ids),
                images=describer is not None,
                speech=transcriber is not None,
            )
            media_text = ""
            if content.images and describer is not None:
                try:
                    media_text = await describer.describe(content.images, content.text)
                except Undescribable as exc:
                    logger.info("{}/{}: pictures skipped: {}", entry.channel_id, entry.msg_id, exc)
            spoken: list[str] = []
            if transcriber is not None:
                for clip in content.speech:
                    try:
                        spoken.append(await transcriber.transcribe(clip))
                    except Untranscribable as exc:
                        logger.info("{}/{}: sound skipped: {}", entry.channel_id, entry.msg_id, exc)
            speech_text = " ".join(s for s in spoken if s)
        except (SessionDead, Busy):
            raise
        except Exception as exc:
            logger.warning(
                "{}/{}: indexing failed (attempt {}): {}",
                entry.channel_id,
                entry.msg_id,
                entry.attempts + 1,
                exc,
            )
            await self._storage.index_fail(entry.id, str(exc), final=last)
            self._retry_at[entry.id] = self._monotonic() + self._retry_s
            return False
        await self._storage.index_done(
            entry.id,
            content.text,
            media_text,
            speech_text,
            fold(f"{content.text}\n{media_text}\n{speech_text}"),
        )
        self._retry_at.pop(entry.id, None)
        return True

    async def run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.drain()
            except SessionDead as exc:
                logger.error("indexing paused, user session dead: {}", exc)
            except Exception:
                logger.exception("indexing pass failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._retry_s)
