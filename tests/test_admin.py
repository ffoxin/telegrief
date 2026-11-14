from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiohttp.test_utils import TestClient, TestServer

from telegrief.admin import Admin
from telegrief.indexer import Indexer, fold
from telegrief.models import ChannelInfo, IndexState, QueueItem
from telegrief.storage import Storage
from tests.fakes import FakeUser

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
CH = ChannelInfo(
    id=-1001, username="a", title="Alpha", subscribers=1, reactions_enabled=True, noforwards=False
)
USER = {"Remote-User": "vital"}


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = await Storage.open(tmp_path / "t.db")
    await s.upsert_channel(CH, NOW)
    yield s
    await s.close()


@pytest.fixture
async def client(storage: Storage) -> AsyncIterator[TestClient]:  # type: ignore[type-arg]
    indexer = Indexer(FakeUser(), storage, None)
    admin = Admin(storage, indexer, ZoneInfo("UTC"))
    async with TestClient(TestServer(admin.app())) as c:
        c.indexer = indexer  # type: ignore[attr-defined]
        yield c


async def add(
    storage: Storage, msg_id: int, text: str = "", pictures: str = "", speech: str = ""
) -> int:
    item = QueueItem(
        folder_id=1,
        channel_id=CH.id,
        msg_ids=(msg_id,),
        date=NOW - timedelta(hours=msg_id),
        engagement=1,
        views=1,
        score=1.0,
        noforwards=False,
        preview="",
        queued_at=NOW,
        sent_msg_id=100 + msg_id,
    )
    await storage.index_add("Memes", -1009000000001, item, NOW)
    entry = (await storage.index_pending())[-1]
    await storage.index_done(
        entry.id, text, pictures, speech, fold(f"{text}\n{pictures}\n{speech}")
    )
    return entry.id


async def test_requests_without_the_proxy_user_are_refused(client: TestClient) -> None:  # type: ignore[type-arg]
    assert (await client.get("/")).status == 401
    assert (await client.post("/post/1/reindex")).status == 401
    assert (await client.get("/", headers=USER)).status == 200


async def test_list_shows_what_was_read_from_each_post(
    client: TestClient,  # type: ignore[type-arg]
    storage: Storage,
) -> None:
    await add(storage, 1, "Рыжий <b>кот</b>", pictures="Кот на диване")
    await add(storage, 2, speech="всем привет, сегодня про ёлку")
    await add(storage, 3)
    page = await (await client.get("/", headers=USER)).text()
    assert "Рыжий &lt;b&gt;кот&lt;/b&gt;" in page  # post text is escaped
    assert "🖼 pictures</span>Кот на диване" in page
    assert "🎙 speech</span>всем привет, сегодня про ёлку" in page
    assert "nothing found" in page and "@a" in page
    assert "https://t.me/a/1" in page and "https://t.me/c/9000000001/101" in page
    assert "<b>3</b> indexed" in page and "<b>1</b> with speech" in page
    assert page.index("https://t.me/a/1") < page.index("https://t.me/a/3")  # newest first


async def test_search_and_filters(client: TestClient, storage: Storage) -> None:  # type: ignore[type-arg]
    await add(storage, 1, "кот")
    await add(storage, 2, speech="про ёлку и кота")
    await add(storage, 3)

    async def shown(**params: str) -> list[str]:
        page = await (await client.get("/", params=params, headers=USER)).text()
        return [f"#{i}" for i in (1, 2, 3) if f">#{i}</a>" in page]

    assert await shown(q="КОТ") == ["#1", "#2"]
    assert await shown(q="кот елку") == ["#2"]
    assert await shown(show="speech") == ["#2"]
    assert await shown(show="nothing") == ["#3"]
    assert await shown(show="failed") == []
    assert await shown(show="bogus", page="x") == ["#1", "#2", "#3"]


async def test_list_is_paged(client: TestClient, storage: Storage) -> None:  # type: ignore[type-arg]
    for i in range(1, 53):
        await add(storage, i, f"пост {i}")
    first = await (await client.get("/", headers=USER)).text()
    second = await (await client.get("/", params={"page": "2"}, headers=USER)).text()
    assert first.count("<article") == 50 and second.count("<article") == 2
    assert "52 posts · page 1 of 2" in first and "/?page=2" in first
    assert "← newer" in second and "older →" not in second


async def test_post_page_and_reindex(client: TestClient, storage: Storage) -> None:  # type: ignore[type-arg]
    entry_id = await add(storage, 1, "x" * 500, speech="привет")
    page = await (await client.get(f"/post/{entry_id}", headers=USER)).text()
    assert "x" * 500 in page and "Re-index this post" in page
    assert (await client.get("/post/999", headers=USER)).status == 404

    cross = await client.post(
        f"/post/{entry_id}/reindex", headers={**USER, "Origin": "https://evil.example"}
    )
    assert cross.status == 403
    assert (await storage.index_get(entry_id)).state == IndexState.DONE  # type: ignore[union-attr]

    done = await client.post(f"/post/{entry_id}/reindex", headers=USER, allow_redirects=False)
    assert done.status == 303 and done.headers["Location"] == f"/post/{entry_id}?queued=1"
    entry = await storage.index_get(entry_id)
    assert entry is not None and entry.state == IndexState.PENDING
    assert entry.speech_text == "привет"  # kept until it is indexed again
    assert client.indexer._wake.is_set()  # type: ignore[attr-defined]
