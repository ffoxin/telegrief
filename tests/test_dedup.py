import io
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from telegrief.dedup import (
    FINE_SIDE,
    Deduper,
    MediaPrint,
    Signature,
    fine_difference,
    match,
    picture_print,
    same_media,
    same_text,
    sign,
    similarity,
    text_sketch,
)
from telegrief.models import FolderConfig, MediaSample, PostSample, QueueItem
from telegrief.storage import Storage
from tests.fakes import FakeUser

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
CH = -1001

# ---- pictures ------------------------------------------------------------------


def picture(seed: int, caption: str = "", size: tuple[int, int] = (800, 640)) -> Image.Image:
    """A busy picture, the same for one seed; ``caption`` goes on a white band above it."""
    rng = random.Random(seed)
    width, height = size
    band = 120 if caption else 0
    image = Image.new("RGB", (width, height + band), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, band, width, height + band), fill=tuple(rng.randrange(256) for _ in "rgb"))
    for _ in range(25):
        x, y = rng.randrange(width), band + rng.randrange(height)
        box = (x, y, x + rng.randrange(40, 300), y + rng.randrange(40, 300))
        draw.ellipse(box, fill=tuple(rng.randrange(256) for _ in "rgb"))
    if caption:
        draw.text((20, 30), caption, fill="black", font=ImageFont.load_default(size=44))
    return image


def jpeg(image: Image.Image, quality: int = 80, side: int | None = None) -> bytes:
    if side is not None:
        scale = side / max(image.size)
        image = image.resize((round(image.width * scale), round(image.height * scale)))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality)
    return out.getvalue()


def printed(data: bytes, media_id: str = "x", duration: int | None = None) -> MediaPrint:
    result = picture_print(data)
    assert result is not None
    return MediaPrint(media_id, duration, *result)


def test_recompressed_and_resized_copies_are_the_same_picture() -> None:
    original = picture(1)
    a = printed(jpeg(original, 85, side=320), "a")
    for copy in (jpeg(original, 40, side=320), jpeg(original, 70, side=800), jpeg(original, 95)):
        assert same_media(a, printed(copy, "b"))


def test_different_pictures_differ() -> None:
    assert not same_media(printed(jpeg(picture(1)), "a"), printed(jpeg(picture(2)), "b"))


def test_one_template_with_two_captions_is_two_pictures() -> None:
    a = printed(jpeg(picture(1, "когда пришёл домой, а там кот"), side=320), "a")
    b = printed(jpeg(picture(1, "понедельник, утро, дедлайн"), side=320), "b")
    assert same_media(a, b, confirm=False)  # the coarse hash cannot tell them apart
    assert not same_media(a, b)  # the fine grid can


def test_fine_difference_is_the_worst_block() -> None:
    a = bytes(FINE_SIDE * FINE_SIDE)
    b = bytearray(a)
    assert fine_difference(a, bytes(b)) == 0
    b[0] = 160  # one cell of the first 4×4 block
    assert fine_difference(a, bytes(b)) == 10


def test_flat_or_broken_pictures_have_no_print() -> None:
    assert picture_print(jpeg(Image.new("RGB", (320, 240), "black"))) is None
    assert picture_print(b"not an image") is None
    assert picture_print(b"") is None


def test_same_file_id_needs_no_picture() -> None:
    assert same_media(MediaPrint("document:7"), MediaPrint("document:7"))
    assert not same_media(MediaPrint("document:7"), MediaPrint("document:8"))


def test_video_covers_match_only_for_the_same_length() -> None:
    cover = jpeg(picture(3), side=320)
    video = printed(cover, "a", duration=14)
    assert same_media(video, printed(cover, "b", duration=15))
    assert not same_media(video, printed(cover, "b", duration=40))
    assert not same_media(video, printed(cover, "b"))  # a photo is not a video


# ---- text ----------------------------------------------------------------------

NEWS = (
    "Сегодня в городе открылся новый мост через реку, движение по нему запустили утром, "
    "а старую переправу закроют на ремонт до конца следующего года"
)
OTHER = (
    "Вчера вечером на стадионе прошёл финальный матч сезона, хозяева поля выиграли со счётом "
    "три один и впервые за десять лет стали чемпионами"
)


def text_sig(text: str, msg_id: int = 1, media: tuple[MediaPrint, ...] = ()) -> Signature:
    words, sketch = text_sketch(text)
    return Signature(CH, msg_id, words, sketch, media)


def test_text_sketch_folds_case_punctuation_and_yo() -> None:
    assert text_sketch("Ёлка, ёлка!  ЗЕЛЁНАЯ") == text_sketch("елка зеленая")
    assert text_sketch("😂 — !") == (0, frozenset())


def test_same_text_allows_a_signature_under_a_long_text() -> None:
    assert same_text(text_sig(NEWS), text_sig(NEWS.upper() + "\n\nПодписаться на канал"))
    assert not same_text(text_sig(NEWS), text_sig(OTHER))
    assert similarity(text_sig(NEWS), text_sig(OTHER)) < 0.2


def test_short_texts_must_match_word_for_word() -> None:
    assert same_text(text_sig("Доброе утро!"), text_sig("доброе   утро"))
    assert not same_text(text_sig("Доброе утро!"), text_sig("Доброе утро, страна"))
    assert not same_text(text_sig("😂"), text_sig("😂"))  # no words: nothing to compare


def test_similarity_of_long_texts_is_estimated_from_the_sketch() -> None:
    words = [f"слово{i}" for i in range(400)]
    a, b = text_sig(" ".join(words)), text_sig(" ".join(words[:380]))
    assert len(a.sketch) == 64
    assert similarity(a, b) == pytest.approx(0.95, abs=0.08)
    assert similarity(a, text_sig(" ".join(f"другое{i}" for i in range(400)))) == 0


# ---- posts ---------------------------------------------------------------------

A = printed(jpeg(picture(11), side=320), "photo:1")
A_COPY = printed(jpeg(picture(11), 50, side=320), "photo:2")
B = printed(jpeg(picture(12), side=320), "photo:3")


def post(*media: MediaPrint, text: str = "", msg_id: int = 1) -> Signature:
    return text_sig(text, msg_id, media)


def test_media_post_repeats_when_every_attachment_was_in_one_post() -> None:
    assert match(post(A_COPY), post(A)) == "media"
    assert match(post(A_COPY), post(A, B)) == "media"  # one picture of a delivered album
    assert match(post(A_COPY, B), post(A)) is None  # the album brings something new
    assert match(post(A_COPY), post(B)) is None
    assert match(post(A_COPY), post()) is None


def test_captions_do_not_matter_unless_both_are_long_and_different() -> None:
    assert match(post(A_COPY, text="жиза"), post(A, text="Подписаться | Прислать мем")) == "media"
    assert match(post(A_COPY, text=NEWS + " @a"), post(A, text=NEWS)) == "media"
    assert match(post(A_COPY, text=NEWS), post(A, text=OTHER)) is None  # a cover over two stories


def test_text_decides_only_without_media() -> None:
    assert match(post(text=NEWS), post(text=NEWS)) == "text"
    assert match(post(text=NEWS), post(text=OTHER)) is None
    assert match(post(B, text=NEWS), post(A, text=NEWS)) is None  # media differs
    assert match(post(text=NEWS), post(A, text=NEWS)) == "text"  # the long text was the post
    assert match(post(text="жиза"), post(A, text="жиза")) is None  # there the picture was


def test_sign_prints_pictures_and_keeps_unreadable_media_by_id() -> None:
    sample = PostSample(
        "Привет, мир",
        (
            MediaSample("photo:1", jpeg(picture(11), side=320)),
            MediaSample("document:2", None, 30),
            MediaSample("photo:3", b"broken"),
        ),
    )
    signature = sign(CH, 5, sample)
    assert (signature.channel_id, signature.msg_id, signature.words) == (CH, 5, 2)
    first, second, third = signature.media
    assert first.coarse is not None and first.fine is not None and len(first.fine) == 1024
    assert second == MediaPrint("document:2", 30)
    assert third == MediaPrint("photo:3")


# ---- Deduper over the database ---------------------------------------------------


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = await Storage.open(tmp_path / "t.db")
    yield s
    await s.close()


def item(folder_id: int, msg_id: int) -> QueueItem:
    return QueueItem(
        folder_id=folder_id,
        channel_id=CH,
        msg_ids=(msg_id,),
        date=NOW - timedelta(hours=30),
        engagement=1,
        views=1,
        score=1.0,
        noforwards=False,
        preview="",
        queued_at=NOW,
    )


async def test_signatures_round_trip_without_fine_grids(storage: Storage) -> None:
    folder = await storage.create_folder("Memes", FolderConfig(), NOW)
    signature = post(A, MediaPrint("document:9", 12), text=NEWS, msg_id=7)
    await storage.save_signature(folder.id, signature, NOW)
    await storage.save_signature(folder.id, signature, NOW)  # saving twice replaces
    (loaded,) = await storage.signatures(folder.id)
    assert loaded.media[0] == MediaPrint("photo:1", None, A.coarse)
    assert loaded.media[1] == MediaPrint("document:9", 12)
    assert (loaded.words, loaded.sketch) == (signature.words, signature.sketch)
    assert await storage.fine_prints(folder.id, CH, 7) == {0: A.fine}
    await storage.delete_folder(folder.id)
    assert await storage.signatures(folder.id) == []


async def test_deduper_finds_a_copy_delivered_earlier(storage: Storage) -> None:
    folder = await storage.create_folder("Memes", FolderConfig(), NOW)
    user = FakeUser()
    deduper = Deduper(user, storage, lambda: NOW)
    user.samples[(CH, 1)] = PostSample("", (MediaSample("photo:1", jpeg(picture(11), side=320)),))
    user.samples[(CH, 2)] = PostSample(
        "", (MediaSample("photo:2", jpeg(picture(11), 50, side=320)),)
    )
    user.samples[(CH, 3)] = PostSample(
        "", (MediaSample("photo:3", jpeg(picture(11, "другая подпись"), side=320)),)
    )

    known = await deduper.known(folder.id)
    first, duplicate = await deduper.check(folder.id, item(folder.id, 1), known)
    assert first is not None and duplicate is None
    await deduper.remember(folder.id, first, known)

    # a later batch: the grids come from the database
    known = await deduper.known(folder.id)
    assert known[0].media[0].fine is None
    _, duplicate = await deduper.check(folder.id, item(folder.id, 2), known)
    assert duplicate is not None
    assert (duplicate.of.msg_id, duplicate.by) == (1, "media")
    recaptioned, duplicate = await deduper.check(folder.id, item(folder.id, 3), known)
    assert recaptioned is not None and duplicate is None


async def test_deduper_lets_an_unreadable_post_through(storage: Storage) -> None:
    folder = await storage.create_folder("Memes", FolderConfig(), NOW)
    deduper = Deduper(FakeUser(), storage, lambda: NOW)
    assert await deduper.check(folder.id, item(folder.id, 1), []) == (None, None)
