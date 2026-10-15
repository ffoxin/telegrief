"""Folder → channels → posts, with album grouping and a channel-info cache."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

from .models import ChannelInfo, Post, RawPost
from .tg.protocols import ChannelUnavailable, UserGateway

if TYPE_CHECKING:
    from .storage import Storage

CACHE_TTL = timedelta(hours=24)


def window(now: datetime, from_h: int, to_h: int) -> tuple[datetime, datetime]:
    """Half-open [now - from_h, now - to_h)."""
    return now - timedelta(hours=from_h), now - timedelta(hours=to_h)


def group_albums(raw: list[RawPost]) -> list[Post]:
    """Merge messages sharing grouped_id into one Post; keep first-seen order."""
    groups: dict[tuple[int, str], list[RawPost]] = {}
    for item in raw:
        key = (item.channel_id, f"g{item.grouped_id}" if item.grouped_id else f"m{item.msg_id}")
        groups.setdefault(key, []).append(item)
    posts: list[Post] = []
    for members in groups.values():
        members.sort(key=lambda m: m.msg_id)
        preview = next((m.preview for m in members if m.preview), "")
        posts.append(
            Post(
                channel_id=members[0].channel_id,
                msg_ids=tuple(m.msg_id for m in members),
                date=min(m.date for m in members),
                views=max(m.views for m in members),
                forwards=max(m.forwards for m in members),
                reactions=sum(m.reactions for m in members),
                replies=max(m.replies for m in members),
                preview=preview,
                noforwards=any(m.noforwards for m in members),
            )
        )
    return posts


@dataclass
class Collected:
    channels: dict[int, ChannelInfo] = field(default_factory=dict)
    posts: list[Post] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Collector:
    def __init__(
        self, user: UserGateway, storage: Storage, cache_ttl: timedelta = CACHE_TTL
    ) -> None:
        self._user = user
        self._storage = storage
        self._ttl = cache_ttl

    async def channel_info(self, channel_id: int, now: datetime) -> ChannelInfo:
        cached = await self._storage.get_channel(channel_id)
        if cached is not None and now - cached[1] < self._ttl:
            return cached[0]
        info = await self._user.channel_info(channel_id)
        await self._storage.upsert_channel(info, now)
        return info

    async def collect(
        self, folder: str, start: datetime, end: datetime, now: datetime
    ) -> Collected:
        result = Collected()
        for channel_id in await self._user.folder_channels(folder):
            try:
                info = await self.channel_info(channel_id, now)
                raw = [
                    replace(p, noforwards=p.noforwards or info.noforwards)
                    async for p in self._user.iter_posts(channel_id, start, end)
                    if start <= p.date < end
                ]
            except ChannelUnavailable as exc:
                logger.warning("folder {}: skipping channel {}: {}", folder, channel_id, exc)
                result.errors.append(f"{channel_id}: {exc}")
                continue
            result.channels[channel_id] = info
            await self._storage.upsert_posts(raw, now)
            result.posts.extend(group_albums(raw))
        return result
