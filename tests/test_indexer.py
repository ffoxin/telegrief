from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegrief.indexer import MAX_ATTEMPTS, Indexer, fold
from telegrief.models import Image, IndexState, PostContent, QueueItem
from telegrief.speech import Untranscribable
from telegrief.storage import Storage
from telegrief.tg.protocols import SessionDead
from telegrief.vision import Undescribable
from tests.fakes import FakeUser

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
CH_ID = -1001
TARGET = -1009000000001
JPEG = Image(b"\xff\xd8jpeg", "image/jpeg")


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = await Storage.open(tmp_path / "t.db")
    yield s
    await s.close()


class FakeDescriber:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []
        self.error: Exception | None = None

    async def describe(self, images: Sequence[Image], text: str) -> str:
        self.calls.append((len(images), text))
        if self.error is not None:
            raise self.error
        return f"описание {len(images)} картинок"


class FakeTranscriber:
    def __init__(self) -> None:
        self.calls: list[bytes] = []
        self.error: Exception | None = None

    async def transcribe(self, clip: bytes) -> str:
        self.calls.append(clip)
        if self.error is not None:
            raise self.error
        return f"сказано {clip.decode()}"


class Harness:
    def __init__(
        self, storage: Storage, *, describer: bool = True, transcriber: bool = True
    ) -> None:
        self.storage = storage
        self.user = FakeUser()
        self.describer = FakeDescriber()
        self.transcriber = FakeTranscriber()
        self.time = 0.0
        self.indexer = Indexer(
            self.user,
            storage,
            self.describer if describer else None,
            self.transcriber if transcriber else None,
            clock=lambda: NOW,
            monotonic=lambda: self.time,
            retry_s=600,
        )

    async def add(
        self, msg_id: int, text: str, *images: Image, speech: tuple[bytes, ...] = ()
    ) -> None:
        self.user.contents[(CH_ID, msg_id)] = PostContent(text=text, images=images, speech=speech)
        item = QueueItem(
            folder_id=1,
            channel_id=CH_ID,
            msg_ids=(msg_id,),
            date=NOW,
            engagement=1,
            views=1,
            score=1.0,
            noforwards=False,
            preview="",
            queued_at=NOW,
            sent_msg_id=msg_id + 50,
        )
        assert await self.indexer.add("Memes", TARGET, item)

    async def found(self, *words: str) -> list[int]:
        entries, _ = await self.storage.index_search([fold(w) for w in words], 10)
        return [e.msg_id for e in entries]


def test_fold_ignores_case_yo_and_spacing() -> None:
    assert fold("  Ёжик\n В   ТУМАНЕ ") == "ежик в тумане"


async def test_text_and_pictures_become_searchable(storage: Storage) -> None:
    h = Harness(storage)
    await h.add(1, "Просто текст")
    await h.add(2, "Подпись", JPEG, JPEG)
    assert await h.found("текст") == []  # nothing is searchable before it is indexed

    assert await h.indexer.drain() == 2

    assert h.describer.calls == [(2, "Подпись")]  # no pictures, no API call
    assert await h.found("ТЕКСТ") == [1]
    assert await h.found("картинок") == [2]
    assert await h.found("подпись", "описание") == [2]
    assert await h.indexer.drain() == 0
    assert await storage.index_counts() == {
        IndexState.PENDING: 0,
        IndexState.DONE: 2,
        IndexState.FAILED: 0,
    }


async def test_same_post_is_indexed_once(storage: Storage) -> None:
    h = Harness(storage)
    await h.add(1, "раз")
    await h.indexer.drain()
    item = (await storage.index_search(["раз"], 1))[0][0]
    assert item.sent_msg_id == 51 and item.target == TARGET and item.folder == "Memes"
    with pytest.raises(AssertionError):
        await h.add(1, "раз")
    assert await h.found("раз") == [1]


async def test_speech_of_every_clip_becomes_searchable(storage: Storage) -> None:
    h = Harness(storage)
    await h.add(1, "Видео", speech=(b"one", b"two"))
    assert await h.indexer.drain() == 1
    assert h.transcriber.calls == [b"one", b"two"] and h.describer.calls == []
    assert await h.found("сказано", "TWO", "видео") == [1]
    (entry,) = (await storage.index_search(["one"], 1))[0]
    assert (entry.media_text, entry.speech_text) == ("", "сказано one сказано two")


async def test_rejected_clip_leaves_the_rest_indexed(storage: Storage) -> None:
    h = Harness(storage)
    h.transcriber.error = Untranscribable("bad audio")
    await h.add(1, "Подпись", JPEG, speech=(b"one",))
    assert await h.indexer.drain() == 1
    assert await h.found("подпись", "описание") == [1]


async def test_without_keys_media_is_not_read(storage: Storage) -> None:
    h = Harness(storage, describer=False, transcriber=False)
    await h.add(1, "Подпись", JPEG, speech=(b"one",))
    assert await h.indexer.drain() == 1
    assert h.user.content_calls == [(CH_ID, [1], False, False)]
    assert await h.found("подпись") == [1]

    only_speech = Harness(storage, describer=False)
    await only_speech.add(2, "Ролик", JPEG, speech=(b"one",))
    assert await only_speech.indexer.drain() == 1
    assert only_speech.user.content_calls == [(CH_ID, [2], False, True)]


async def test_refused_pictures_leave_the_text_indexed(storage: Storage) -> None:
    h = Harness(storage)
    h.describer.error = Undescribable("refused")
    await h.add(1, "Подпись", JPEG)
    assert await h.indexer.drain() == 1
    assert await h.found("подпись") == [1]


async def test_failures_wait_then_fall_back_to_text_only(storage: Storage) -> None:
    h = Harness(storage)
    h.describer.error = RuntimeError("overloaded")
    await h.add(1, "Подпись", JPEG)

    assert await h.indexer.drain() == 0
    assert await h.indexer.drain() == 0  # not due yet: no second attempt
    assert len(h.describer.calls) == 1
    (entry,) = await storage.index_pending()
    assert (entry.attempts, entry.error) == (1, "overloaded")

    for attempt in range(2, MAX_ATTEMPTS):
        h.time += 600
        assert await h.indexer.drain() == 0
        assert len(h.describer.calls) == attempt
    h.time += 600
    assert await h.indexer.drain() == 1  # last attempt leaves the pictures out
    assert len(h.describer.calls) == MAX_ATTEMPTS - 1
    assert h.user.content_calls[-1] == (CH_ID, [1], False, False)
    assert await h.found("подпись") == [1]


async def test_post_that_stays_unreadable_ends_failed(storage: Storage) -> None:
    h = Harness(storage)
    await h.add(1, "Подпись")
    del h.user.contents[(CH_ID, 1)]
    for _ in range(MAX_ATTEMPTS):
        assert await h.indexer.drain() == 0
        h.time += 600
    assert await storage.index_pending() == []
    assert (await storage.index_counts())[IndexState.FAILED] == 1
    assert await storage.index_retry_failed() == 1
    assert [e.attempts for e in await storage.index_pending()] == [0]


async def test_dead_session_is_not_counted_as_an_attempt(storage: Storage) -> None:
    h = Harness(storage)
    await h.add(1, "Подпись")
    h.user.dead = True
    with pytest.raises(SessionDead):
        await h.indexer.drain()
    assert [e.attempts for e in await storage.index_pending()] == [0]
