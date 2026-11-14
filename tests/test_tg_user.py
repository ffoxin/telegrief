from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.tl import types

from telegrief.config import normalize_title
from telegrief.tg.protocols import PING_TEXT, FolderNotFound
from telegrief.tg.user import (
    TelethonUser,
    add_pinned_peer,
    channel_peer_ids,
    compose_caption,
    filter_title,
    has_sound,
    match_folder,
    pick_photo_size,
    raw_post_from_message,
    remove_peer,
    select_channel_ids,
)

T = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def test_filter_title_handles_plain_str() -> None:
    assert filter_title(SimpleNamespace(title="Memes")) == "Memes"


def test_filter_title_handles_text_with_entities() -> None:
    assert filter_title(SimpleNamespace(title=SimpleNamespace(text="Memes 🐸"))) == "Memes 🐸"


def test_normalize_title_strips_emoji_and_case() -> None:
    assert normalize_title("🔥 Memes 🔥") == "memes"
    assert normalize_title("Memes\ufe0f") == "memes"
    assert normalize_title("  My   News 📰 ") == "my news"
    assert normalize_title("1️⃣ News 🇺🇸") == "1 news"
    assert normalize_title("👨‍👩‍👧 Family") == "family"


def _filter(title: str) -> SimpleNamespace:
    return SimpleNamespace(title=SimpleNamespace(text=title))


def test_match_folder_prefers_exact_title() -> None:
    filters = [_filter("memes"), _filter("Memes")]
    assert match_folder(filters, "Memes") is filters[1]


def test_match_folder_ignores_emoji_and_case() -> None:
    filters = [_filter("News 📰"), _filter("🔥 Memes 🔥")]
    assert match_folder(filters, "memes") is filters[1]
    assert match_folder(filters, "News") is filters[0]


def test_match_folder_ambiguous_lists_candidates() -> None:
    filters = [_filter("🔥 Memes"), _filter("Memes 🐸")]
    with pytest.raises(FolderNotFound, match="ambiguous: 🔥 Memes · Memes 🐸"):
        match_folder(filters, "Memes")


def test_match_folder_missing() -> None:
    with pytest.raises(FolderNotFound, match="Memes"):
        match_folder([_filter("News")], "Memes")


def test_select_channel_ids_union_minus_exclude_keeps_order() -> None:
    ids = select_channel_ids(
        include=[-1003, -1001],
        pinned=[-1002],
        exclude=[-1003],
        broadcasts=False,
        all_broadcasts=[-1009],
    )
    assert ids == [-1002, -1001]


def test_select_channel_ids_broadcasts_flag_adds_all() -> None:
    ids = select_channel_ids(
        include=[-1001],
        pinned=[],
        exclude=[-1009],
        broadcasts=True,
        all_broadcasts=[-1001, -1008, -1009],
    )
    assert ids == [-1001, -1008]


def message(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "id": 10,
        "date": T,
        "grouped_id": None,
        "views": 500,
        "forwards": 3,
        "reactions": SimpleNamespace(results=[SimpleNamespace(count=4), SimpleNamespace(count=6)]),
        "replies": SimpleNamespace(replies=2),
        "message": "line one\nline two " + "x" * 100,
        "noforwards": False,
        "action": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_raw_post_from_message_maps_fields() -> None:
    post = raw_post_from_message(message(), -1001)
    assert post is not None
    assert post.channel_id == -1001 and post.msg_id == 10 and post.date == T
    assert post.views == 500 and post.forwards == 3 and post.reactions == 10 and post.replies == 2
    assert post.preview.startswith("line one line two ") and len(post.preview) == 80
    assert post.noforwards is False


def test_raw_post_from_message_keeps_full_text() -> None:
    post = raw_post_from_message(message(), -1001)
    assert post is not None
    assert post.text == "line one line two " + "x" * 100
    assert post.preview == post.text[:80]


def test_raw_post_from_message_handles_missing_counters() -> None:
    post = raw_post_from_message(
        message(views=None, forwards=None, reactions=None, replies=None, message=None), -1001
    )
    assert post is not None
    assert (post.views, post.forwards, post.reactions, post.replies, post.preview) == (
        0,
        0,
        0,
        0,
        "",
    )


def test_raw_post_from_message_empty_reactions_results() -> None:
    post = raw_post_from_message(message(reactions=SimpleNamespace(results=[])), -1001)
    assert post is not None and post.reactions == 0


def test_raw_post_from_message_skips_service_messages() -> None:
    assert raw_post_from_message(message(action=SimpleNamespace()), -1001) is None


def test_channel_peer_ids_keeps_only_channels() -> None:
    peers = [
        types.InputPeerChannel(5, 0),
        types.InputPeerSelf(),  # "Saved Messages" in a folder
        types.InputPeerUser(9, 0),
        types.InputPeerChat(3),
        types.InputPeerChannel(6, 0),
    ]
    assert channel_peer_ids(peers) == [-1000000000005, -1000000000006]


def test_compose_caption_appends_full_text_and_truncates() -> None:
    head = "↗️ @memes · https://t.me/memes/10"
    assert compose_caption(head, "") == head
    assert compose_caption(head, "hello") == head + "\n\nhello"
    assert len(compose_caption(head, "x" * 5000)) == 1024


async def test_ping_bot_sends_ping_text(tmp_path: Path) -> None:
    sent: list[tuple[str, str]] = []

    class _Client:
        async def send_message(self, peer: str, text: str) -> None:
            sent.append((peer, text))

    user = TelethonUser(_Client(), 1, tmp_path)  # type: ignore[arg-type]
    await user.ping_bot("telegrief_bot")
    assert sent == [("telegrief_bot", PING_TEXT)]


def test_add_pinned_peer_prepends_once() -> None:
    old = types.InputPeerChannel(1, 11)
    new = types.InputPeerChannel(2, 22)
    dialog_filter = SimpleNamespace(pinned_peers=[old], include_peers=[old, types.InputPeerSelf()])
    assert add_pinned_peer(dialog_filter, new) is True
    assert dialog_filter.pinned_peers == [new, old]
    assert dialog_filter.include_peers == [new, old, types.InputPeerSelf()]
    assert add_pinned_peer(dialog_filter, types.InputPeerChannel(2, 99)) is False
    assert add_pinned_peer(dialog_filter, old) is False
    assert len(dialog_filter.pinned_peers) == 2


def test_remove_peer_drops_channel_from_pinned_and_included() -> None:
    a, b = types.InputPeerChannel(1, 11), types.InputPeerChannel(2, 22)
    dialog_filter = SimpleNamespace(
        pinned_peers=[b, a], include_peers=[b, a, types.InputPeerSelf()]
    )
    assert remove_peer(dialog_filter, -1000000000002) is True
    assert dialog_filter.pinned_peers == [a]
    assert dialog_filter.include_peers == [a, types.InputPeerSelf()]
    assert remove_peer(dialog_filter, -1000000000002) is False


def _size(kind: str, side: int) -> types.PhotoSize:
    return types.PhotoSize(type=kind, w=side, h=side // 2, size=side * 10)


def test_pick_photo_size_prefers_the_smallest_legible_one() -> None:
    stripped = types.PhotoStrippedSize(type="i", bytes=b"x")
    sizes = [stripped, _size("w", 2560), _size("m", 320), _size("y", 1280), _size("x", 800)]
    assert pick_photo_size(sizes).type == "y"
    assert pick_photo_size(sizes[2:3] + sizes[4:]).type == "x"  # nothing large: the largest
    assert pick_photo_size([stripped]) is None


def _doc(mime: str, *attributes: object) -> SimpleNamespace:
    return SimpleNamespace(mime_type=mime, attributes=list(attributes))


def test_has_sound_skips_gifs_stickers_and_muted_videos() -> None:
    video = types.DocumentAttributeVideo(duration=5, w=1, h=1)
    muted = types.DocumentAttributeVideo(duration=5, w=1, h=1, nosound=True)
    assert has_sound(_doc("video/mp4", video))
    assert has_sound(_doc("audio/ogg", types.DocumentAttributeAudio(duration=5, voice=True)))
    assert not has_sound(_doc("video/mp4", muted))
    assert not has_sound(_doc("video/mp4", video, types.DocumentAttributeAnimated()))
    assert not has_sound(_doc("image/webp"))
    assert not has_sound(_doc(""))


class FakeDownloadClient:
    """get_messages returns the given messages; download_media saves like Telethon does."""

    def __init__(self, messages: list[SimpleNamespace]) -> None:
        self.messages = messages
        self.saved: list[Path] = []

    async def get_messages(self, channel_id: int, ids: list[int]) -> list[SimpleNamespace]:
        return self.messages

    async def download_media(self, msg: SimpleNamespace, file: str) -> str:
        saved = Path(file + ".mp4")  # the extension is appended to the requested path
        saved.write_bytes(b"video")
        self.saved.append(saved)
        return str(saved)


async def test_post_content_cuts_speech_from_the_file_telethon_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = types.DocumentAttributeVideo(duration=5, w=1, h=1)
    messages = [
        SimpleNamespace(
            id=7, message="Смотри", media=object(), photo=None, document=_doc("video/mp4", video)
        ),
        SimpleNamespace(id=8, message="", media=None, photo=None, document=None),
    ]
    messages[0].document.size = 1000
    client = FakeDownloadClient(messages)
    clipped: list[tuple[Path, bool]] = []

    async def fake_clip(path: Path) -> bytes:
        clipped.append((path, path.exists()))
        return b"OggS"

    monkeypatch.setattr("telegrief.tg.user.speech_clip", fake_clip)
    user = TelethonUser(client, me_id=1, tmp_dir=tmp_path / "tmp")  # type: ignore[arg-type]

    content = await user.post_content(-1001, [7, 8], images=False, speech=True)

    assert (content.text, content.speech, content.images) == ("Смотри", (b"OggS",), ())
    assert clipped == [(client.saved[0], True)]
    assert list((tmp_path / "tmp").iterdir()) == []  # the download is removed afterwards

    messages[0].document.size = 51 * 1024 * 1024
    assert (await user.post_content(-1001, [7], images=False, speech=True)).speech == ()
