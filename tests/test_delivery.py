from datetime import date

import pytest

from telegrief.config import FolderConfig
from telegrief.delivery import (
    Deliverer,
    bare_id,
    build_caption,
    post_link,
    render_footer,
    render_title,
)
from telegrief.models import ChannelInfo, Delivery, DigestItem
from telegrief.tg.protocols import TargetUnavailable
from tests.fakes import FakeBot, FakeUser

PUBLIC = ChannelInfo(
    id=-1001234,
    username="memes",
    title="Memes",
    subscribers=10,
    reactions_enabled=True,
    noforwards=False,
)
PRIVATE = ChannelInfo(
    id=-1005678,
    username=None,
    title="Secret <Club> & co",
    subscribers=10,
    reactions_enabled=False,
    noforwards=True,
)
TARGET = -100900


def item(
    rank: int = 1,
    *,
    channel: ChannelInfo = PUBLIC,
    msg_ids: tuple[int, ...] = (10,),
    noforwards: bool = False,
    preview: str = "lol",
) -> DigestItem:
    return DigestItem(
        digest_id=1,
        rank=rank,
        channel_id=channel.id,
        msg_ids=msg_ids,
        engagement=120,
        views=2900,
        score=0.041,
        noforwards=noforwards,
        preview=preview,
    )


def test_bare_id() -> None:
    assert bare_id(-1001234567890) == 1234567890
    assert bare_id(1234) == 1234


def test_post_link_public_and_private() -> None:
    assert post_link(PUBLIC, 10) == "https://t.me/memes/10"
    assert post_link(PRIVATE, 10) == "https://t.me/c/5678/10"


def test_build_caption_is_source_line() -> None:
    assert build_caption(PUBLIC, 10) == "↗️ @memes · https://t.me/memes/10"
    assert build_caption(PRIVATE, 10) == "↗️ Secret <Club> & co · https://t.me/c/5678/10"


def test_render_title_escapes_html() -> None:
    assert render_title("A <b> & co", date(2026, 10, 14), 1, 1).startswith(
        "📂 A &lt;b&gt; &amp; co ·"
    )


def test_render_title() -> None:
    assert render_title("Memes", date(2026, 10, 14), 57, 23) == (
        "📂 Memes · 2026-10-14 · 57 posts from 23 channels"
    )
    assert render_title("Memes", date(2026, 10, 14), 0, 0) == (
        "📂 Memes · 2026-10-14 · no posts in window"
    )


def test_render_footer_lines_and_counter() -> None:
    cfg = FolderConfig(name="Memes")
    text = render_footer(
        [item(1), item(2, channel=PRIVATE)],
        {PUBLIC.id: PUBLIC, PRIVATE.id: PRIVATE},
        cfg,
        shown=10,
        total=57,
        done=False,
    )
    assert text.splitlines() == [
        "#1 · 4.1% · 120👍 / 2.9k👁 · @memes",
        "#2 · 4.1% · 120↗️ / 2.9k👁 · Secret &lt;Club&gt; &amp; co",
        "shown 10/57",
    ]


def test_render_footer_escapes_html() -> None:
    cfg = FolderConfig(name="Memes")
    text = render_footer([item(1, channel=PRIVATE)], {PRIVATE.id: PRIVATE}, cfg, 1, 1, True)
    assert "<Club>" not in text and "&lt;Club&gt;" in text


def test_render_footer_done_lines() -> None:
    cfg = FolderConfig(name="Memes")
    full = render_footer([item(1)], {PUBLIC.id: PUBLIC}, cfg, shown=57, total=57, done=True)
    assert full.splitlines()[-1] == "✅ all 57 shown"
    partial = render_footer([item(1)], {PUBLIC.id: PUBLIC}, cfg, shown=55, total=57, done=True)
    assert partial.splitlines()[-1] == "✅ done · 55/57 shown"


def test_render_footer_unknown_channel_uses_id() -> None:
    cfg = FolderConfig(name="Memes")
    text = render_footer([item(1)], {}, cfg, 1, 1, False)
    assert text.splitlines()[0].endswith(f"· {PUBLIC.id}")


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def user(bot: FakeBot) -> FakeUser:
    user = FakeUser()
    user.bot = bot
    return user


async def test_deliver_forwards_via_relay(user: FakeUser, bot: FakeBot) -> None:
    deliverer = Deliverer(user, bot, max_copy_bytes=10)
    kind, last_id = await deliverer.deliver(TARGET, item(msg_ids=(10, 11, 12)), PUBLIC)
    assert kind == Delivery.FORWARD
    assert user.forwarded == [(PUBLIC.id, [10, 11, 12])]
    # the bot works with ids from its own id-space, never the user's relay ids
    assert bot.forwards == [(TARGET, [7000, 7001, 7002])]
    assert bot.deleted == [[7000, 7001, 7002]]
    assert bot.cleared == 1
    assert last_id == 3  # three messages landed in TARGET, ids 1..3


async def test_deliver_relay_timeout_falls_back_to_link(user: FakeUser, bot: FakeBot) -> None:
    user.bot = None  # relay messages never reach the bot
    deliverer = Deliverer(user, bot, max_copy_bytes=10)
    kind, _ = await deliverer.deliver(TARGET, item(), PUBLIC)
    assert kind == Delivery.LINK
    assert bot.texts(TARGET) == ["https://t.me/memes/10"]
    assert user.copied == []  # copying would time out the same way; go straight to link


async def test_deliver_restricted_error_falls_back_to_copy(user: FakeUser, bot: FakeBot) -> None:
    user.restricted.add(PUBLIC.id)
    deliverer = Deliverer(user, bot, max_copy_bytes=10)
    kind, last_id = await deliverer.deliver(TARGET, item(), PUBLIC)
    assert kind == Delivery.COPY
    assert user.copied == [(PUBLIC.id, [10], "↗️ @memes · https://t.me/memes/10")]
    assert bot.resent == [(TARGET, [7000])]
    assert bot.deleted == [[7000]]
    assert last_id == 1


async def test_deliver_noforwards_item_skips_forward_attempt(user: FakeUser, bot: FakeBot) -> None:
    deliverer = Deliverer(user, bot, max_copy_bytes=10)
    kind, _ = await deliverer.deliver(TARGET, item(noforwards=True), PUBLIC)
    assert kind == Delivery.COPY
    assert user.forwarded == []


async def test_deliver_too_large_falls_back_to_link(user: FakeUser, bot: FakeBot) -> None:
    user.media_bytes[(PUBLIC.id, 10)] = 11
    deliverer = Deliverer(user, bot, max_copy_bytes=10)
    kind, last_id = await deliverer.deliver(TARGET, item(noforwards=True), PUBLIC)
    assert kind == Delivery.LINK
    assert bot.texts(TARGET) == ["https://t.me/memes/10"]
    assert last_id == 1
    assert bot.deleted == []


async def test_deliver_copy_failure_falls_back_to_link(user: FakeUser, bot: FakeBot) -> None:
    user.copy_fails.add(PRIVATE.id)
    deliverer = Deliverer(user, bot, max_copy_bytes=10)
    kind, _ = await deliverer.deliver(TARGET, item(channel=PRIVATE, noforwards=True), PRIVATE)
    assert kind == Delivery.LINK
    assert bot.texts(TARGET) == ["https://t.me/c/5678/10"]


async def test_deliver_target_error_propagates(user: FakeUser, bot: FakeBot) -> None:
    bot.bad_targets.add(TARGET)
    deliverer = Deliverer(user, bot, max_copy_bytes=10)
    with pytest.raises(TargetUnavailable):
        await deliverer.deliver(TARGET, item(), PUBLIC)
    assert bot.deleted == [[7000]]  # relay copy is cleaned up even on failure
