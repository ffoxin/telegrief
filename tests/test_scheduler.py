from pathlib import Path

import pytest

from telegrief.scheduler import Heartbeat, parse_run_at


def test_parse_run_at() -> None:
    assert parse_run_at("02:00") == (2, 0)
    assert parse_run_at("23:59") == (23, 59)


def test_parse_run_at_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_run_at("2am")


def test_heartbeat_touch_creates_and_updates(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    hb = Heartbeat(path)
    hb.touch()
    assert path.exists()
    first = path.stat().st_mtime
    hb.touch()
    assert path.stat().st_mtime >= first
