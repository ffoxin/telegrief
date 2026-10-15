"""Deliver one digest item into a target channel and render title/footer texts."""

from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable
from datetime import date

from loguru import logger

from .config import FolderConfig
from .models import ChannelInfo, Delivery, DigestItem
from .scoring import format_count, format_engagement, format_score
from .tg.protocols import (
    BotGateway,
    CopyFailed,
    ForwardRestricted,
    GatewayError,
    RelayTimeout,
    TargetUnavailable,
    UserGateway,
)

RELAY_TIMEOUT_S = 30.0


def bare_id(channel_id: int) -> int:
    text = str(channel_id)
    return int(text[4:]) if text.startswith("-100") else abs(channel_id)


def post_link(channel: ChannelInfo, msg_id: int) -> str:
    if channel.username:
        return f"https://t.me/{channel.username}/{msg_id}"
    return f"https://t.me/c/{bare_id(channel.id)}/{msg_id}"


def build_caption(channel: ChannelInfo, msg_id: int) -> str:
    """Source line for a copied protected post; the gateway appends the original text."""
    return f"↗️ {channel.display} · {post_link(channel, msg_id)}"


def render_title(folder: str, day: date, total: int, n_channels: int) -> str:
    name = html.escape(folder)
    if total == 0:
        return f"📂 {name} · {day:%Y-%m-%d} · no posts in window"
    return f"📂 {name} · {day:%Y-%m-%d} · {total} posts from {n_channels} channels"


def _placeholder(channel_id: int) -> ChannelInfo:
    return ChannelInfo(
        id=channel_id,
        username=None,
        title=str(channel_id),
        subscribers=0,
        reactions_enabled=True,
        noforwards=False,
    )


def render_footer(
    batch: list[DigestItem],
    channels: dict[int, ChannelInfo],
    cfg: FolderConfig,
    shown: int,
    total: int,
    done: bool,
) -> str:
    lines: list[str] = []
    for item in batch:
        channel = channels.get(item.channel_id) or _placeholder(item.channel_id)
        lines.append(
            f"#{item.rank} · {format_score(item.score, cfg.scoring)} · "
            f"{format_engagement(item.engagement, channel)} / {format_count(item.views)}👁 · "
            f"{html.escape(channel.display)}"
        )
    if done and shown >= total:
        lines.append(f"✅ all {total} shown")
    elif done:
        lines.append(f"✅ done · {shown}/{total} shown")
    else:
        lines.append(f"shown {shown}/{total}")
    return "\n".join(lines)


class Deliverer:
    """Forward, else copy, else link.

    Message ids in a private chat are per-account: the ids the user session gets
    back from sending to the bot are not the ids the bot sees. So the bot learns
    relay ids from its own incoming updates; one item goes through the relay at a
    time so the arrivals are unambiguous.
    """

    def __init__(self, user: UserGateway, bot: BotGateway, max_copy_bytes: int) -> None:
        self._user = user
        self._bot = bot
        self._max_copy_bytes = max_copy_bytes
        self._relay_lock = asyncio.Lock()

    async def _via_relay(
        self,
        send: Callable[[], Awaitable[list[int]]],
        use: Callable[[list[int]], Awaitable[list[int]]],
    ) -> int:
        async with self._relay_lock:
            self._bot.clear_relay()
            user_ids = await send()
            relay = await self._bot.await_relay(len(user_ids), RELAY_TIMEOUT_S)
            try:
                sent = await use(relay)
            finally:
                await self._bot.delete_relay(relay)
        if not sent:
            raise CopyFailed("bot delivered nothing")
        return max(sent)

    async def deliver(
        self, target: int, item: DigestItem, channel: ChannelInfo
    ) -> tuple[Delivery, int]:
        """Returns (kind, last message id in target). Raises TargetUnavailable."""
        msg_ids = list(item.msg_ids)
        first = item.msg_ids[0]
        if not item.noforwards:
            try:
                last = await self._via_relay(
                    lambda: self._user.forward_to_relay(item.channel_id, msg_ids),
                    lambda relay: self._bot.forward_from_relay(target, relay),
                )
                return Delivery.FORWARD, last
            except ForwardRestricted:
                logger.info("{}/{}: forward restricted, copying", item.channel_id, first)
            except RelayTimeout as exc:
                logger.warning(
                    "{}/{}: relay timeout ({}), sending link", item.channel_id, first, exc
                )
                return Delivery.LINK, await self._link(target, channel, first)
        head = build_caption(channel, first)
        try:
            last = await self._via_relay(
                lambda: self._user.copy_to_relay(
                    item.channel_id, msg_ids, head, self._max_copy_bytes
                ),
                lambda relay: self._bot.resend_from_relay(target, relay),
            )
            return Delivery.COPY, last
        except TargetUnavailable:
            raise
        except GatewayError as exc:
            logger.info("{}/{}: copy skipped ({}), sending link", item.channel_id, first, exc)
        return Delivery.LINK, await self._link(target, channel, first)

    async def _link(self, target: int, channel: ChannelInfo, msg_id: int) -> int:
        return await self._bot.send_text(target, post_link(channel, msg_id), preview=True)
