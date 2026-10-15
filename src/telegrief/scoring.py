"""Engagement selection, scoring strategies, ranking and number formatting."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .models import ChannelInfo, FolderConfig, Post, ScoredPost, Scoring

Strategy = Callable[[int, int, int, FolderConfig], float]


def engagement(post: Post, channel: ChannelInfo) -> int:
    """Reactions normally; forwards for channels that have reactions disabled."""
    return post.reactions if channel.reactions_enabled else post.forwards


def _raw(e: int, views: int, subscribers: int, cfg: FolderConfig) -> float:
    return float(e)


def _per_subscriber(e: int, views: int, subscribers: int, cfg: FolderConfig) -> float:
    return e / max(subscribers, cfg.min_denominator) * 1000


def _per_view(e: int, views: int, subscribers: int, cfg: FolderConfig) -> float:
    return e / max(views, cfg.min_denominator)


STRATEGIES: dict[Scoring, Strategy] = {
    "raw": _raw,
    "per_subscriber": _per_subscriber,
    "per_view": _per_view,
}


def score(e: int, views: int, subscribers: int, cfg: FolderConfig) -> float:
    """Score one post from its engagement, views and the channel's subscriber count."""
    return STRATEGIES[cfg.scoring](e, views, subscribers, cfg)


def rank(
    posts: Iterable[Post], channels: dict[int, ChannelInfo], cfg: FolderConfig
) -> list[ScoredPost]:
    """Score, drop zero-engagement posts, sort best first."""
    scored: list[ScoredPost] = []
    for post in posts:
        channel = channels.get(post.channel_id)
        if channel is None:
            continue
        e = engagement(post, channel)
        if e <= 0:
            continue
        scored.append(ScoredPost(post, channel, e, score(e, post.views, channel.subscribers, cfg)))
    scored.sort(key=lambda s: (-s.score, -s.engagement, s.post.date))
    return scored


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
