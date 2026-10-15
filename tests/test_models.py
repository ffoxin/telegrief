from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from telegrief.models import FolderConfig, QueueItem

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)


def test_folder_config_defaults() -> None:
    cfg = FolderConfig()
    assert (cfg.min_age_h, cfg.period_h, cfg.batch_size) == (24, 24, 10)
    assert cfg.scoring == "per_view"
    assert (cfg.min_denominator, cfg.max_items, cfg.max_age_d) == (200, 1000, 7)


def test_folder_config_window() -> None:
    assert FolderConfig().window(NOW) == (NOW - timedelta(hours=48), NOW - timedelta(hours=24))
    cfg = FolderConfig(min_age_h=24, period_h=6)
    assert cfg.window(NOW) == (NOW - timedelta(hours=30), NOW - timedelta(hours=24))
    assert FolderConfig(min_age_h=0, period_h=12).window(NOW)[1] == NOW


def test_folder_config_rejects_max_age_not_covering_window() -> None:
    with pytest.raises(ValidationError, match="max_age"):
        FolderConfig(min_age_h=24, period_h=24, max_age_d=2)
    assert FolderConfig(min_age_h=24, period_h=24, max_age_d=3).max_age_d == 3


def test_folder_config_rejects_bad_values() -> None:
    for bad in ({"period_h": 0}, {"batch_size": 0}, {"scoring": "magic"}, {"max_age_d": 0}):
        with pytest.raises(ValidationError):
            FolderConfig(**bad)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        FolderConfig(batchsize=3)  # type: ignore[call-arg]


def test_folder_config_coerces_strings() -> None:
    cfg = FolderConfig.model_validate({"batch_size": "20", "min_age_h": "0"})
    assert cfg.batch_size == 20 and cfg.min_age_h == 0


def test_queue_item_msg_id_is_first_of_album() -> None:
    item = QueueItem(
        folder_id=1,
        channel_id=-1001,
        msg_ids=(10, 11),
        date=NOW,
        engagement=5,
        views=100,
        score=0.05,
        noforwards=False,
        preview="",
        queued_at=NOW,
    )
    assert item.msg_id == 10
