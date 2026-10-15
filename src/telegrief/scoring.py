"""Engagement selection, scoring strategies, ranking and number formatting."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .config import FolderConfig, Scoring
from .models import ChannelInfo, Post, ScoredPost

Strategy = Callable[[int, Post, ChannelInfo, FolderConfig], float]


def engagement(post: Post, channel: ChannelInfo) -> int:
    """Reactions normally; forwards for channels that have reactions disabled."""
    return post.reactions if channel.reactions_enabled else post.forwards


def _raw(e: int, post: Post, channel: ChannelInfo, cfg: FolderConfig) -> float:
    return float(e)


def _per_subscriber(e: int, post: Post, channel: ChannelInfo, cfg: FolderConfig) -> float:
    return e / max(channel.subscribers, cfg.min_denominator) * 1000


def _per_view(e: int, post: Post, channel: ChannelInfo, cfg: FolderConfig) -> float:
    return e / max(post.views, cfg.min_denominator)


STRATEGIES: dict[Scoring, Strategy] = {
    "raw": _raw,
    "per_subscriber": _per_subscriber,
    "per_view": _per_view,
}


def rank(
    posts: Iterable[Post], channels: dict[int, ChannelInfo], cfg: FolderConfig
) -> list[ScoredPost]:
    """Score, drop zero-engagement posts, sort best first, cut to max_items."""
    strategy = STRATEGIES[cfg.scoring]
    scored: list[ScoredPost] = []
    for post in posts:
        channel = channels.get(post.channel_id)
        if channel is None:
            continue
        e = engagement(post, channel)
        if e <= 0:
            continue
        scored.append(ScoredPost(post, channel, e, strategy(e, post, channel, cfg)))
    scored.sort(key=lambda s: (-s.score, -s.engagement, s.post.date))
    return scored[: cfg.max_items]


def format_count(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def format_score(score: float, scoring: Scoring) -> str:
    if scoring == "raw":
        return str(int(score))
    if scoring == "per_subscriber":
        return f"{score:.1f}/1k"
    return f"{score * 100:.1f}%"


def format_engagement(e: int, channel: ChannelInfo) -> str:
    return f"{format_count(e)}{'👍' if channel.reactions_enabled else '↗️'}"
