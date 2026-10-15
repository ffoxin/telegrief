"""Deliver one digest item into a target channel and render title/footer texts."""

from __future__ import annotations

import html
from datetime import date

from loguru import logger

from .config import FolderConfig
from .models import ChannelInfo, Delivery, DigestItem
from .scoring import format_count, format_engagement, format_score
from .tg.protocols import BotGateway, ForwardRestricted, GatewayError, UserGateway

CAPTION_LIMIT = 1024


def bare_id(channel_id: int) -> int:
    text = str(channel_id)
    return int(text[4:]) if text.startswith("-100") else abs(channel_id)


def post_link(channel: ChannelInfo, msg_id: int) -> str:
    if channel.username:
        return f"https://t.me/{channel.username}/{msg_id}"
    return f"https://t.me/c/{bare_id(channel.id)}/{msg_id}"


def build_caption(channel: ChannelInfo, msg_id: int, preview: str) -> str:
    head = f"↗️ {channel.display} · {post_link(channel, msg_id)}"
    text = f"{head}\n\n{preview}" if preview else head
    return text[:CAPTION_LIMIT]


def render_title(folder: str, day: date, total: int, n_channels: int) -> str:
    if total == 0:
        return f"📂 {folder} · {day:%Y-%m-%d} · no posts in window"
    return f"📂 {folder} · {day:%Y-%m-%d} · {total} posts from {n_channels} channels"


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
    def __init__(self, user: UserGateway, bot: BotGateway, max_copy_bytes: int) -> None:
        self._user = user
        self._bot = bot
        self._max_copy_bytes = max_copy_bytes

    async def deliver(
        self, target: int, item: DigestItem, channel: ChannelInfo
    ) -> tuple[Delivery, int]:
        """Forward, else copy, else link. Returns (kind, last message id in target)."""
        msg_ids = list(item.msg_ids)
        first = item.msg_ids[0]
        if not item.noforwards:
            try:
                relay = await self._user.forward_to_relay(item.channel_id, msg_ids)
                sent = await self._bot.forward_from_relay(target, relay)
                await self._bot.delete_relay(relay)
                return Delivery.FORWARD, max(sent)
            except ForwardRestricted:
                logger.info("{}/{}: forward restricted, copying", item.channel_id, first)
        caption = build_caption(channel, first, item.preview)
        try:
            relay = await self._user.copy_to_relay(
                item.channel_id, msg_ids, caption, self._max_copy_bytes
            )
        except GatewayError as exc:
            logger.info("{}/{}: copy skipped ({}), sending link", item.channel_id, first, exc)
        else:
            sent = await self._bot.resend_from_relay(target, relay, caption)
            await self._bot.delete_relay(relay)
            return Delivery.COPY, max(sent)
        link_id = await self._bot.send_text(target, post_link(channel, first), preview=True)
        return Delivery.LINK, link_id
