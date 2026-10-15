from datetime import UTC, datetime
from types import SimpleNamespace

from telethon.tl import types

from telegrief.tg.user import (
    channel_peer_ids,
    compose_caption,
    filter_title,
    raw_post_from_message,
    select_channel_ids,
)

T = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def test_filter_title_handles_plain_str() -> None:
    assert filter_title(SimpleNamespace(title="Memes")) == "Memes"


def test_filter_title_handles_text_with_entities() -> None:
    assert filter_title(SimpleNamespace(title=SimpleNamespace(text="Memes 🐸"))) == "Memes 🐸"


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
