"""Core data types shared by every module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True)
class ChannelInfo:
    id: int  # marked id, e.g. -1001234567890
    username: str | None
    title: str
    subscribers: int
    reactions_enabled: bool
    noforwards: bool

    @property
    def display(self) -> str:
        return f"@{self.username}" if self.username else self.title


@dataclass(frozen=True)
class RawPost:
    channel_id: int
    msg_id: int
    grouped_id: int | None
    date: datetime
    views: int
    forwards: int
    reactions: int
    replies: int
    preview: str
    noforwards: bool


@dataclass(frozen=True)
class Post:
    """One digest candidate: a single message or a whole album."""

    channel_id: int
    msg_ids: tuple[int, ...]
    date: datetime
    views: int
    forwards: int
    reactions: int
    replies: int
    preview: str
    noforwards: bool

    @property
    def msg_id(self) -> int:
        return self.msg_ids[0]


@dataclass(frozen=True)
class ScoredPost:
    post: Post
    channel: ChannelInfo
    engagement: int
    score: float


class DigestStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    FAILED = "failed"


class ItemState(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class Delivery(StrEnum):
    FORWARD = "forward"
    COPY = "copy"
    LINK = "link"


@dataclass
class Digest:
    id: int
    folder: str
    target: int
    window_start: datetime
    window_end: datetime
    total: int
    status: DigestStatus
    created_at: datetime
    title_msg_id: int | None = None
    footer_msg_id: int | None = None
    footer_text: str | None = None
    last_batch_at: datetime | None = None


@dataclass
class DigestItem:
    digest_id: int
    rank: int
    channel_id: int
    msg_ids: tuple[int, ...]
    engagement: int
    views: int
    score: float
    noforwards: bool
    preview: str
    state: ItemState = ItemState.PENDING
    delivery: Delivery | None = None
    sent_msg_id: int | None = None
    sent_at: datetime | None = None
