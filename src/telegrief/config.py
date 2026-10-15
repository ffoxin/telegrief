"""Environment settings (pydantic-settings) and folder-title normalization."""

from __future__ import annotations

import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_secrets_dir() -> str | None:
    path = Path("/run/secrets")
    return str(path) if path.is_dir() else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir=_default_secrets_dir(), extra="ignore")

    tg_api_id: int
    tg_api_hash: str
    bot_token: str
    tz: str = "UTC"
    auto_next_min_interval_s: int = Field(default=120, ge=0)
    # Share of the current batch's posts that must be read before the next batch
    # is sent automatically (1.0 = every post).
    auto_next_read_fraction: float = Field(default=0.9, gt=0, le=1)
    max_copy_mb: int = Field(default=200, ge=0)
    data_dir: Path = Path("/app/data")
    log_level: str = "INFO"

    @field_validator("tz")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    @property
    def max_copy_bytes(self) -> int:
        return self.max_copy_mb * 1024 * 1024

    @property
    def db_path(self) -> Path:
        return self.data_dir / "telegrief.db"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat"

    @property
    def user_session(self) -> Path:
        return self.data_dir / "user"

    @property
    def bot_session(self) -> Path:
        return self.data_dir / "bot"


# Unicode categories dropped when comparing folder names: symbols (emoji, dingbats,
# regional-indicator flags), format chars (ZWJ), nonspacing marks (variation selectors),
# enclosing marks (keycap) and surrogates.
_IGNORED_CATEGORIES = frozenset({"So", "Sk", "Cf", "Mn", "Me", "Cs"})


def strip_symbols(title: str) -> str:
    """Title without emoji/symbols, whitespace collapsed, case kept."""
    kept = "".join(c for c in title if unicodedata.category(c) not in _IGNORED_CATEGORIES)
    return " ".join(kept.split())


def normalize_title(title: str) -> str:
    """Case-insensitive comparison key: ``strip_symbols`` plus casefold."""
    return strip_symbols(title).casefold()


def load_settings() -> Settings:
    """Build Settings from env / secrets; required fields come from the environment."""
    return Settings()  # type: ignore[call-arg]
