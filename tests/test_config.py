from pathlib import Path

import pytest
from pydantic import ValidationError

from telegrief.config import Settings, normalize_title, strip_symbols

REQUIRED = {"TG_API_ID": "123", "TG_API_HASH": "abc", "BOT_TOKEN": "tok"}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)


def test_settings_from_env(env: None) -> None:
    s = Settings(_secrets_dir=None)
    assert s.tg_api_id == 123
    assert s.tz == "UTC"
    assert s.max_copy_bytes == 200 * 1024 * 1024
    assert s.db_path == Path("/app/data/telegrief.db")
    assert s.heartbeat_path == Path("/app/data/heartbeat")
    assert s.auto_next_read_fraction == 0.9
    assert s.auto_next_min_interval_s == 120


def test_settings_ignore_removed_variables(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_AT", "02:00")
    monkeypatch.setenv("WINDOW_FROM_H", "48")
    s = Settings(_secrets_dir=None)
    assert not hasattr(s, "run_at")


def test_settings_from_secrets_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "tg_api_id").write_text("42\n")
    (tmp_path / "tg_api_hash").write_text("hash\n")
    (tmp_path / "bot_token").write_text("token\n")
    s = Settings(_secrets_dir=str(tmp_path))
    assert s.tg_api_id == 42
    assert s.bot_token == "token"


def test_settings_rejects_bad_read_fraction(env: None) -> None:
    with pytest.raises(ValidationError):
        Settings(_secrets_dir=None, auto_next_read_fraction=0)
    with pytest.raises(ValidationError):
        Settings(_secrets_dir=None, auto_next_read_fraction=1.5)


def test_settings_rejects_bad_tz(env: None) -> None:
    with pytest.raises(ValidationError):
        Settings(_secrets_dir=None, tz="Mars/Olympus")


def test_strip_symbols_keeps_case_and_words() -> None:
    assert strip_symbols("😊mEm") == "mEm"
    assert strip_symbols("📰  My   News 🇺🇸") == "My News"
    assert strip_symbols("🔥🔥") == ""


def test_normalize_title_folds_case() -> None:
    assert normalize_title("😊mEm") == "mem"
    assert normalize_title("  MEM ") == "mem"
