"""Environment settings (pydantic-settings) and folders.yaml model."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Scoring = Literal["raw", "per_subscriber", "per_view"]

_RUN_AT = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _default_secrets_dir() -> str | None:
    path = Path("/run/secrets")
    return str(path) if path.is_dir() else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir=_default_secrets_dir(), extra="ignore")

    tg_api_id: int
    tg_api_hash: str
    bot_token: str
    tz: str = "UTC"
    run_at: str = "02:00"
    window_from_h: int = Field(default=48, ge=1)
    window_to_h: int = Field(default=24, ge=0)
    auto_next_min_interval_s: int = Field(default=120, ge=0)
    max_copy_mb: int = Field(default=200, ge=0)
    data_dir: Path = Path("/app/data")
    folders_file: Path = Path("/app/folders.yaml")
    log_level: str = "INFO"

    @field_validator("run_at")
    @classmethod
    def _check_run_at(cls, value: str) -> str:
        if not _RUN_AT.match(value):
            raise ValueError("RUN_AT must be HH:MM (24h)")
        return value

    @field_validator("tz")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    @model_validator(mode="after")
    def _check_window(self) -> Settings:
        if self.window_from_h <= self.window_to_h:
            raise ValueError("WINDOW_FROM_H must be greater than WINDOW_TO_H")
        return self

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


class FolderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    batch_size: int = Field(default=10, ge=1)
    scoring: Scoring = "per_view"
    min_denominator: int = Field(default=200, ge=1)
    max_items: int = Field(default=1000, ge=1)
    target: int | None = None


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


class FoldersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folders: list[FolderConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names(self) -> FoldersConfig:
        names = [normalize_title(f.name) for f in self.folders]
        if len(names) != len(set(names)):
            raise ValueError("folder names must be unique (ignoring emoji and case)")
        return self

    def get(self, name: str) -> FolderConfig | None:
        """Folder by name: exact match first, then ignoring emoji, case and spacing."""
        exact = next((f for f in self.folders if f.name == name), None)
        if exact is not None:
            return exact
        wanted = normalize_title(name)
        return next((f for f in self.folders if normalize_title(f.name) == wanted), None)


def load_folders(path: Path) -> FoldersConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return FoldersConfig.model_validate(data)


def load_settings() -> Settings:
    """Build Settings from env / secrets; required fields come from the environment."""
    return Settings()  # type: ignore[call-arg]
