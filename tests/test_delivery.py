from datetime import UTC, datetime

import pytest

from telegrief.delivery import (
    Deliverer,
    bare_id,
    build_caption,
    post_link,
    render_fetch_note,
    render_footer,
)
from telegrief.models import ChannelInfo, Delivery, FolderConfig, QueueItem
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
NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)


def item(
    msg_ids: tuple[int, ...] = (10,),
    *,
    channel: ChannelInfo = PUBLIC,
    noforwards: bool = False,
    preview: str = "lol",
) -> QueueItem:
    return QueueItem(
        folder_id=1,
        channel_id=channel.id,
        msg_ids=msg_ids,
        date=NOW,
        engagement=120,
        views=2900,
        score=0.041,
        noforwards=noforwards,
        preview=preview,
        queued_at=NOW,
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


def test_render_footer_lines_and_counter() -> None:
    text = render_footer(
        [item(), item((20,), channel=PRIVATE)],
        {PUBLIC.id: PUBLIC, PRIVATE.id: PRIVATE},
        FolderConfig(),
        pending=47,
    )
    assert text.splitlines() == [
        "#1 · 4.1% · 120👍 / 2.9k👁 · @memes",
        "#2 · 4.1% · 120↗️ / 2.9k👁 · Secret &lt;Club&gt; &amp; co",
        "queued 47",
    ]


def test_render_footer_escapes_html() -> None:
    text = render_footer([item(channel=PRIVATE)], {PRIVATE.id: PRIVATE}, FolderConfig(), 1)
    assert "<Club>" not in text and "&lt;Club&gt;" in text


def test_render_footer_empty_queue() -> None:
    text = render_footer([item()], {PUBLIC.id: PUBLIC}, FolderConfig(), pending=0)
    assert text.splitlines()[-1] == "✅ queue empty"


def test_render_footer_unknown_channel_uses_id() -> None:
    text = render_footer([item()], {}, FolderConfig(), 1)
    assert text.splitlines()[0].endswith(f"· {PUBLIC.id}")


def test_render_fetch_note() -> None:
    assert render_fetch_note(37, 120) == "📥 +37 · 120 queued"


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
    kind, last_id = await deliverer.deliver(TARGET, item((10, 11, 12)), PUBLIC)
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
