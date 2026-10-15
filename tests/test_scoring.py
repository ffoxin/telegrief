from datetime import UTC, datetime

from telegrief.models import ChannelInfo, FolderConfig, Post
from telegrief.scoring import (
    engagement,
    format_count,
    format_engagement,
    format_score,
    rank,
    score,
)

CH = ChannelInfo(
    id=-1001, username="a", title="A", subscribers=10_000, reactions_enabled=True, noforwards=False
)
CH_NO_REACT = ChannelInfo(
    id=-1002, username=None, title="B", subscribers=500, reactions_enabled=False, noforwards=False
)
T0 = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def post(
    msg_id: int = 1,
    *,
    channel_id: int = -1001,
    views: int = 1000,
    forwards: int = 0,
    reactions: int = 50,
    date: datetime = T0,
) -> Post:
    return Post(
        channel_id=channel_id,
        msg_ids=(msg_id,),
        date=date,
        views=views,
        forwards=forwards,
        reactions=reactions,
        replies=0,
        preview="",
        noforwards=False,
    )


def test_engagement_uses_reactions_when_enabled() -> None:
    assert engagement(post(reactions=7, forwards=99), CH) == 7


def test_engagement_uses_forwards_when_reactions_disabled() -> None:
    assert engagement(post(reactions=7, forwards=99), CH_NO_REACT) == 99


def test_per_view_applies_floor() -> None:
    cfg = FolderConfig(scoring="per_view", min_denominator=200)
    [scored] = rank([post(views=50, reactions=10)], {-1001: CH}, cfg)
    assert scored.score == 10 / 200


def test_per_view_without_floor() -> None:
    cfg = FolderConfig(scoring="per_view", min_denominator=200)
    [scored] = rank([post(views=1000, reactions=41)], {-1001: CH}, cfg)
    assert scored.score == 0.041


def test_per_subscriber() -> None:
    cfg = FolderConfig(scoring="per_subscriber", min_denominator=200)
    [scored] = rank([post(reactions=50)], {-1001: CH}, cfg)
    assert scored.score == 5.0  # 50 / 10_000 * 1000


def test_raw() -> None:
    cfg = FolderConfig(scoring="raw")
    [scored] = rank([post(reactions=50)], {-1001: CH}, cfg)
    assert scored.score == 50.0


def test_rank_drops_zero_engagement() -> None:
    cfg = FolderConfig()
    posts = [post(1, reactions=0), post(2, reactions=1)]
    assert [s.post.msg_id for s in rank(posts, {-1001: CH}, cfg)] == [2]


def test_rank_drops_posts_from_unknown_channels() -> None:
    cfg = FolderConfig()
    assert rank([post(1, channel_id=-1099)], {-1001: CH}, cfg) == []


def test_rank_orders_by_score_then_engagement_then_date() -> None:
    cfg = FolderConfig(scoring="per_view", min_denominator=1)
    later = T0.replace(hour=13)
    posts = [
        post(1, views=1000, reactions=10),  # 0.01
        post(2, views=100, reactions=10),  # 0.10, engagement 10
        post(3, views=200, reactions=20, date=later),  # 0.10, engagement 20, later
        post(4, views=200, reactions=20),  # 0.10, engagement 20, earlier
    ]
    assert [s.post.msg_id for s in rank(posts, {-1001: CH}, cfg)] == [4, 3, 2, 1]


def test_rank_keeps_everything() -> None:
    cfg = FolderConfig(max_items=2)
    posts = [post(i, reactions=i) for i in range(1, 6)]
    assert [s.post.msg_id for s in rank(posts, {-1001: CH}, cfg)] == [5, 4, 3, 2, 1]


def test_score_matches_rank() -> None:
    cfg = FolderConfig(scoring="per_subscriber", min_denominator=200)
    assert score(50, 1000, 10_000, cfg) == 5.0
    assert score(10, 50, 10_000, FolderConfig(min_denominator=200)) == 10 / 200
    assert score(7, 1, 1, FolderConfig(scoring="raw")) == 7.0


def test_format_count() -> None:
    assert format_count(950) == "950"
    assert format_count(1000) == "1.0k"
    assert format_count(2900) == "2.9k"
    assert format_count(1_200_000) == "1.2M"


def test_format_score() -> None:
    assert format_score(120.0, "raw") == "120"
    assert format_score(3.44, "per_subscriber") == "3.4/1k"
    assert format_score(0.041, "per_view") == "4.1%"


def test_format_engagement() -> None:
    assert format_engagement(120, CH) == "120👍"
    assert format_engagement(2900, CH_NO_REACT) == "2.9k↗️"
