"""Duplicates: a post whose text, pictures or videos a folder's digest already got.

A post is checked right before delivery against the folder's delivered posts, so of
several copies the best-scored one is delivered and the rest are dropped.

Media decides for a post that has any: it is a duplicate when every attachment was in
one delivered post. Two attachments are the same when Telegram's file id is (a forward,
a re-send without upload), or when their pictures are: the photo itself, or the cover
of a video plus its length. Pictures are compared in two steps: a 64-bit difference
hash finds candidates, then a 32×32 greyscale grid confirms them block by block, which
tells a recompressed copy from the same meme template with another caption.

Text decides for a post without media: the same words, or, for longer texts, four
fifths of them (a channel's signature under a copied text). Under a picture, text only
vetoes: the same cover over two long, different texts is two posts.

The thresholds are calibrated on synthetic recompressed, resized and re-captioned
pictures; they lean to missing a duplicate rather than hiding a new post.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from loguru import logger
from PIL import Image

from .models import PostSample, QueueItem
from .tg.protocols import SessionDead, UserGateway

if TYPE_CHECKING:
    from .storage import Storage

_WORD = re.compile(r"\w+")

# Text: a sketch is the SKETCH_SIZE smallest word hashes, enough to estimate the share
# of common words (exact for texts of up to SKETCH_SIZE distinct words).
SKETCH_SIZE = 64
SAME_TEXT = 0.8  # share of common words that makes two texts one
NEAR_MIN_WORDS = 10  # shorter texts must match word for word
LONG_TEXT_WORDS = 15  # a caption this long says something of its own
DIFFERENT_TEXT = 0.5  # below this share two long captions are two posts

# Pictures. Measured on recompressed / resized copies: coarse distance <= 3 of 64,
# fine difference < 5; on one template with two captions: fine difference > 11.
FINE_SIDE = 32
FINE_BLOCK = 4
COARSE_MAX_DISTANCE = 10
FINE_MAX_DIFFERENCE = 8.0
MIN_CONTRAST = 32  # a flat picture (a black first frame) looks like every other one
MAX_PIXELS = 50_000_000
DURATION_SLACK_S = 1

MatchKind = Literal["media", "text"]


@dataclass(frozen=True)
class MediaPrint:
    id: str
    duration: int | None = None
    coarse: int | None = None  # difference hash, 64 bits
    fine: bytes | None = None  # FINE_SIDE × FINE_SIDE grey levels; loaded on demand


@dataclass(frozen=True)
class Signature:
    """What is compared of a post: its words and a print of each attachment."""

    channel_id: int
    msg_id: int
    words: int = 0  # distinct words in the text
    sketch: frozenset[int] = frozenset()
    media: tuple[MediaPrint, ...] = ()


@dataclass(frozen=True)
class Duplicate:
    of: Signature
    by: MatchKind


# ---- text --------------------------------------------------------------------


def text_sketch(text: str) -> tuple[int, frozenset[int]]:
    """Number of distinct words (case and ё/е folded) and the smallest of their hashes."""
    words = set(_WORD.findall(text.casefold().replace("ё", "е")))
    hashes = sorted(
        int.from_bytes(hashlib.blake2b(w.encode(), digest_size=4).digest(), "big") for w in words
    )
    return len(words), frozenset(hashes[:SKETCH_SIZE])


def similarity(a: Signature, b: Signature) -> float:
    """Share of the distinct words of two texts that both have (Jaccard), from 0 to 1."""
    common = a.sketch & b.sketch
    if not common:
        return 0.0
    union = a.sketch | b.sketch
    if len(union) <= SKETCH_SIZE:
        return len(common) / len(union)
    return sum(1 for h in sorted(union)[:SKETCH_SIZE] if h in common) / SKETCH_SIZE


def same_text(a: Signature, b: Signature) -> bool:
    if not a.words or not b.words:
        return False
    share = similarity(a, b)
    if min(a.words, b.words) < NEAR_MIN_WORDS:
        return share == 1.0
    return share >= SAME_TEXT


# ---- pictures ----------------------------------------------------------------


def picture_print(data: bytes) -> tuple[int, bytes] | None:
    """(coarse hash, fine grid) of an image file; None when unreadable or nearly flat."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_PIXELS:
                return None
            grey = image.convert("L")
    except Exception:  # Pillow raises a dozen unrelated types on a broken file
        return None
    grid = grey.resize((FINE_SIDE, FINE_SIDE), Image.Resampling.BOX).tobytes()
    low, high = min(grid), max(grid)
    if high - low < MIN_CONTRAST:
        return None
    fine = bytes((level - low) * 255 // (high - low) for level in grid)
    # difference hash: is each pixel of an 8-row, 9-column rendition brighter than the next
    rows = grey.resize((9, 8), Image.Resampling.LANCZOS).tobytes()
    coarse = 0
    for y in range(8):
        for x in range(8):
            coarse = coarse << 1 | (rows[y * 9 + x] > rows[y * 9 + x + 1])
    return coarse, fine


def fine_difference(a: bytes, b: bytes) -> float:
    """Mean grey-level difference in the most different block of two fine grids.

    Recompression spreads a little noise everywhere; another caption changes a few
    blocks a lot. The worst block tells them apart where a mean over the picture cannot.
    """
    worst = 0
    for top in range(0, FINE_SIDE, FINE_BLOCK):
        for left in range(0, FINE_SIDE, FINE_BLOCK):
            total = 0
            for y in range(top, top + FINE_BLOCK):
                start = y * FINE_SIDE + left
                for i in range(start, start + FINE_BLOCK):
                    total += abs(a[i] - b[i])
            worst = max(worst, total)
    return worst / (FINE_BLOCK * FINE_BLOCK)


def same_media(a: MediaPrint, b: MediaPrint, *, confirm: bool = True) -> bool:
    """The same file, or the same picture (for videos: the same cover and length).

    ``confirm=False`` stops at the coarse hash: a candidate whose fine grid is not loaded.
    """
    if a.id == b.id:
        return True
    if a.coarse is None or b.coarse is None:
        return False
    if (a.duration is None) != (b.duration is None):
        return False
    if (
        a.duration is not None
        and b.duration is not None
        and abs(a.duration - b.duration) > DURATION_SLACK_S
    ):
        return False
    if (a.coarse ^ b.coarse).bit_count() > COARSE_MAX_DISTANCE:
        return False
    if not confirm:
        return True
    if a.fine is None or b.fine is None:
        return False
    return fine_difference(a.fine, b.fine) <= FINE_MAX_DIFFERENCE


# ---- posts -------------------------------------------------------------------


def sign(channel_id: int, msg_id: int, sample: PostSample) -> Signature:
    words, sketch = text_sketch(sample.text)
    media: list[MediaPrint] = []
    for item in sample.media:
        printed = picture_print(item.picture) if item.picture else None
        coarse, fine = printed if printed is not None else (None, None)
        media.append(MediaPrint(item.id, item.duration, coarse, fine))
    return Signature(channel_id, msg_id, words, sketch, tuple(media))


def match(post: Signature, known: Signature, *, confirm: bool = True) -> MatchKind | None:
    """How ``post`` repeats the delivered ``known``, or None when it does not."""
    if post.media:
        if not all(any(same_media(m, n, confirm=confirm) for n in known.media) for m in post.media):
            return None
        long_texts = min(post.words, known.words) >= LONG_TEXT_WORDS
        if long_texts and similarity(post, known) < DIFFERENT_TEXT:
            return None
        return "media"
    if known.media and known.words < LONG_TEXT_WORDS:
        return None  # a caption is not what that post was about
    return "text" if same_text(post, known) else None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Deduper:
    """Signs posts and finds them among the signatures of a folder's delivered posts.

    Never fails a batch: a post that cannot be read or compared is delivered.
    """

    def __init__(
        self, user: UserGateway, storage: Storage, clock: Callable[[], datetime] = _utcnow
    ) -> None:
        self._user = user
        self._storage = storage
        self._clock = clock

    async def known(self, folder_id: int) -> list[Signature]:
        """Signatures of the folder's delivered posts, fine grids left in the database."""
        try:
            return await self._storage.signatures(folder_id)
        except Exception:
            logger.exception("folder {}: cannot load signatures", folder_id)
            return []

    async def check(
        self, folder_id: int, item: QueueItem, known: list[Signature]
    ) -> tuple[Signature | None, Duplicate | None]:
        """The post's signature (None when unreadable) and what it duplicates, if anything."""
        try:
            sample = await self._user.post_sample(item.channel_id, list(item.msg_ids))
            signature = await asyncio.to_thread(sign, item.channel_id, item.msg_id, sample)
            return signature, await self._find(folder_id, signature, known)
        except SessionDead:
            raise
        except Exception as exc:
            logger.warning(
                "{}/{}: not checked for duplicates: {}", item.channel_id, item.msg_id, exc
            )
            return None, None

    async def _find(
        self, folder_id: int, signature: Signature, known: list[Signature]
    ) -> Duplicate | None:
        for candidate in known:
            if match(signature, candidate, confirm=False) is None:
                continue
            full = await self._with_fine(folder_id, candidate)
            by = match(signature, full)
            if by is not None:
                return Duplicate(full, by)
        return None

    async def _with_fine(self, folder_id: int, signature: Signature) -> Signature:
        if all(m.fine is not None or m.coarse is None for m in signature.media):
            return signature
        grids = await self._storage.fine_prints(folder_id, signature.channel_id, signature.msg_id)
        return replace(
            signature,
            media=tuple(
                replace(m, fine=grids.get(position, m.fine))
                for position, m in enumerate(signature.media)
            ),
        )

    async def remember(self, folder_id: int, signature: Signature, known: list[Signature]) -> None:
        """A delivered post: later ones are compared with it, in this batch and after."""
        known.append(signature)
        try:
            await self._storage.save_signature(folder_id, signature, self._clock())
        except Exception:
            logger.exception("{}/{}: signature not saved", signature.channel_id, signature.msg_id)
