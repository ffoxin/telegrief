"""Core data types shared by every module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Scoring = Literal["raw", "per_subscriber", "per_view"]


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


class FolderConfig(BaseModel):
    """Per-folder tuning, editable through /folder."""

    model_config = ConfigDict(extra="forbid")

    min_age_h: int = Field(default=24, ge=0)
    period_h: int = Field(default=24, ge=1)
    batch_size: int = Field(default=10, ge=1)
    scoring: Scoring = "per_view"
    min_denominator: int = Field(default=200, ge=1)
    max_items: int = Field(default=1000, ge=1)
    max_age_d: int = Field(default=7, ge=1)

    @model_validator(mode="after")
    def _max_age_covers_window(self) -> FolderConfig:
        if self.max_age_d * 24 <= self.min_age_h + self.period_h:
            raise ValueError("max_age (days) must exceed min_age + period (hours)")
        return self

    def window(self, at: datetime) -> tuple[datetime, datetime]:
        """Half-open fetch window ``[at - (min_age + period), at - min_age)``."""
        return (
            at - timedelta(hours=self.min_age_h + self.period_h),
            at - timedelta(hours=self.min_age_h),
        )


@dataclass
class Folder:
    id: int
    name: str
    config: FolderConfig
    next_run_at: datetime
    created_at: datetime
    last_fetch_at: datetime | None = None
    last_batch_at: datetime | None = None
    last_error: str | None = None
    fetch_msg_id: int | None = None
    fetch_added: int = 0
    button_msg_id: int | None = None
    button_text: str | None = None


@dataclass
class QueueItem:
    """One queued post (or album) of a folder."""

    folder_id: int
    channel_id: int
    msg_ids: tuple[int, ...]
    date: datetime
    engagement: int
    views: int
    score: float
    noforwards: bool
    preview: str
    queued_at: datetime
    state: ItemState = ItemState.PENDING
    delivery: Delivery | None = None
    sent_msg_id: int | None = None
    sent_at: datetime | None = None

    @property
    def msg_id(self) -> int:
        return self.msg_ids[0]
