from pathlib import Path

import pytest
from pydantic import ValidationError

from telegrief.config import FolderConfig, FoldersConfig, Settings, load_folders, strip_symbols

REQUIRED = {"TG_API_ID": "123", "TG_API_HASH": "abc", "BOT_TOKEN": "tok"}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)


def test_settings_from_env(env: None) -> None:
    s = Settings(_secrets_dir=None)
    assert s.tg_api_id == 123
    assert s.run_at == "02:00"
    assert s.window_from_h == 48 and s.window_to_h == 24
    assert s.max_copy_bytes == 200 * 1024 * 1024
    assert s.db_path == Path("/app/data/telegrief.db")
    assert s.heartbeat_path == Path("/app/data/heartbeat")


def test_settings_from_secrets_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "tg_api_id").write_text("42\n")
    (tmp_path / "tg_api_hash").write_text("hash\n")
    (tmp_path / "bot_token").write_text("token\n")
    s = Settings(_secrets_dir=str(tmp_path))
    assert s.tg_api_id == 42
    assert s.bot_token == "token"


def test_settings_rejects_bad_run_at(env: None) -> None:
    with pytest.raises(ValidationError):
        Settings(_secrets_dir=None, run_at="2:00")


def test_settings_rejects_bad_tz(env: None) -> None:
    with pytest.raises(ValidationError):
        Settings(_secrets_dir=None, tz="Mars/Olympus")


def test_settings_rejects_inverted_window(env: None) -> None:
    with pytest.raises(ValidationError):
        Settings(_secrets_dir=None, window_from_h=24, window_to_h=48)


def test_settings_allows_window_to_zero(env: None) -> None:
    s = Settings(_secrets_dir=None, window_from_h=24, window_to_h=0)
    assert s.window_to_h == 0


def test_folder_defaults(tmp_path: Path) -> None:
    path = tmp_path / "folders.yaml"
    path.write_text("folders:\n  - name: Memes\n")
    cfg = load_folders(path)
    folder = cfg.folders[0]
    assert folder == FolderConfig(name="Memes")
    assert folder.batch_size == 10
    assert folder.scoring == "per_view"
    assert folder.min_denominator == 200
    assert folder.max_items == 1000
    assert folder.target is None
    assert cfg.get("Memes") is folder
    assert cfg.get("Nope") is None


def test_strip_symbols_keeps_case_and_words() -> None:
    assert strip_symbols("😊mEm") == "mEm"
    assert strip_symbols("📰  My   News 🇺🇸") == "My News"
    assert strip_symbols("🔥🔥") == ""


def test_folders_get_ignores_emoji_and_case() -> None:
    folder = FolderConfig(name="😊mEm")
    cfg = FoldersConfig(folders=[folder, FolderConfig(name="📰 News")])
    assert cfg.get("mem") is folder
    assert cfg.get("MEM") is folder
    assert cfg.get("news") is cfg.folders[1]


def test_folders_reject_names_equal_after_normalization() -> None:
    with pytest.raises(ValueError, match="unique"):
        FoldersConfig(folders=[FolderConfig(name="🔥 Memes"), FolderConfig(name="memes")])


def test_folder_overrides(tmp_path: Path) -> None:
    path = tmp_path / "folders.yaml"
    path.write_text(
        "folders:\n"
        "  - name: News\n"
        "    batch_size: 15\n"
        "    scoring: per_subscriber\n"
        "    min_denominator: 1000\n"
        "    max_items: 50\n"
        "    target: -1001234567890\n"
    )
    folder = load_folders(path).folders[0]
    assert folder.batch_size == 15
    assert folder.scoring == "per_subscriber"
    assert folder.target == -1001234567890


def test_folders_duplicate_names_rejected() -> None:
    with pytest.raises(ValidationError):
        FoldersConfig(folders=[FolderConfig(name="A"), FolderConfig(name="A")])


def test_folders_empty_rejected() -> None:
    with pytest.raises(ValidationError):
        FoldersConfig(folders=[])


def test_folders_unknown_key_rejected(tmp_path: Path) -> None:
    path = tmp_path / "folders.yaml"
    path.write_text("folders:\n  - name: Memes\n    batchsize: 3\n")
    with pytest.raises(ValidationError):
        load_folders(path)


def test_folders_bad_scoring_rejected() -> None:
    with pytest.raises(ValidationError):
        FolderConfig(name="A", scoring="magic")  # type: ignore[arg-type]
