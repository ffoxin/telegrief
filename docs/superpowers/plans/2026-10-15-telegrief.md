# telegrief Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Daily service that collects posts from channels in chosen Telegram folders, ranks them by normalized engagement, and forwards them in read-paced batches into auto-created private digest channels.

**Architecture:** One long-running process with two Telethon clients (user session reads and relays, bot delivers and talks to the owner), an apscheduler cron job, and SQLite. Everything outside `telegrief/tg/` depends only on two `Protocol` gateways, so the orchestrator, scoring, storage and commands are unit-tested with in-memory fakes.

**Tech Stack:** Python 3.12, Telethon 1.45.0 + cryptg, aiosqlite, apscheduler 3.11, pydantic-settings, PyYAML, loguru, click; uv, ruff, mypy, pytest + pytest-asyncio; Docker (`python:3.12-slim`), docker compose, GitHub Actions → GHCR.

**Spec:** `docs/superpowers/specs/2026-10-15-telegrief-design.md`

## Global Constraints

- Python `>=3.12`; dependency pins: `telethon==1.45.0`, `cryptg==0.6.0`, `aiosqlite==0.22.1`, `apscheduler==3.11.3`, `pydantic==2.14.1`, `pydantic-settings==2.16.0`, `pyyaml==6.0.3`, `loguru==0.7.3`, `click==8.5.0`; dev: `ruff==0.17.0`, `mypy==2.4.0`, `pytest==9.1.1`, `pytest-asyncio==1.4.0`, `types-pyyaml==6.0.12.20260906`; uv `0.13.1`.
- Package lives in `src/telegrief/`, console script `telegrief`, migrations in `src/telegrief/migrations/NNN_name.sql`.
- All channel ids in the app are Telethon "marked" ids (`-100…`). Gateways convert.
- All datetimes are timezone-aware UTC; stored as ISO strings.
- Every message the bot posts into a digest channel is `silent=True`; footers and titles use HTML parse mode with link previews off; link-fallback messages have previews on.
- Defaults copied from the spec: `RUN_AT=02:00`, `TZ=UTC`, `WINDOW_FROM_H=48`, `WINDOW_TO_H=24`, `AUTO_NEXT_MIN_INTERVAL_S=120`, `MAX_COPY_MB=200`, `batch_size=10`, `scoring=per_view`, `min_denominator=200`, `max_items=1000`.
- Auto-created digest channel title: `"{folder} · digest"`. Caption for copied protected posts: `↗️ {channel} · {link}` + original text, cut at 1024 chars.
- Commits: `git commit` messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Repo author is `Vital Kolas <ffoxin@gmail.com>` (already set in `.git/config`).
- `ruff format`, `ruff check`, `mypy` (strict) and `pytest` must pass before every commit: run `./check.sh lint` (autoformat + autofix) and then `./check.sh`. Code blocks in this plan may exceed the 100-column limit; let `ruff format` rewrap them.

## Review Focus

1. Folder title arriving as `TextWithEntities` (newer layers) instead of `str` — must still match the configured name. Test in Task 11 (`test_filter_title_handles_text_with_entities`).
2. Channel titles containing `<`, `>` or `&` in the HTML footer — must be escaped, not break the message. Test in Task 8 (`test_render_footer_escapes_html`).
3. Read-marker event for a channel that is not any digest target (user reads some other channel) — must be ignored silently. Test in Task 9 (`test_on_read_ignores_unknown_channel`).
4. Folder removed from `folders.yaml` while its digest is still open — `send_batch` must return 0 and not crash on a `None` config. Test in Task 9 (`test_send_batch_without_folder_config_is_noop`).
5. `/run` with a folder name containing spaces and trailing hour arguments (`/run My Memes 24 0`) — folder must be `My Memes`, hours `24`/`0`. Test in Task 10 (`test_run_parses_folder_with_spaces`).

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore`, `LICENSE`, `README.md`, `check.sh`, `src/telegrief/__init__.py`, `src/telegrief/py.typed`, `tests/__init__.py`, `tests/test_smoke.py`

**Interfaces:**
- Produces: importable package `telegrief` with `__version__`; `uv.lock`; `./check.sh` running ruff/mypy/pytest.

- [ ] **Step 1: Write pyproject.toml**

```toml
[project]
name = "telegrief"
version = "0.1.0"
description = "Daily ranked digest of Telegram channel posts from your folders"
readme = "README.md"
license = { text = "MIT" }
requires-python = ">=3.12"
dependencies = [
    "telethon==1.45.0",
    "cryptg==0.6.0",
    "aiosqlite==0.22.1",
    "apscheduler==3.11.3",
    "pydantic==2.14.1",
    "pydantic-settings==2.16.0",
    "pyyaml==6.0.3",
    "loguru==0.7.3",
    "click==8.5.0",
]

[project.scripts]
telegrief = "telegrief.cli:main"

[dependency-groups]
dev = [
    "ruff==0.17.0",
    "mypy==2.4.0",
    "pytest==9.1.1",
    "pytest-asyncio==1.4.0",
    "types-pyyaml==6.0.12.20260906",
]

[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/telegrief"]

[tool.ruff]
line-length = 100
target-version = "py312"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "RUF"]
ignore = ["RUF001", "RUF002", "RUF003"]

[tool.mypy]
python_version = "3.12"
strict = true
files = ["src"]

[[tool.mypy.overrides]]
module = ["telethon.*", "apscheduler.*"]
ignore_missing_imports = true

[[tool.mypy.overrides]]
module = ["telegrief.tg.user", "telegrief.tg.bot"]
disallow_untyped_calls = false
warn_return_any = false

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Write the remaining scaffold files**

`.python-version`:

```
3.12
```

`.gitignore`:

```
.venv/
__pycache__/
*.pyc
.mypy_cache/
.ruff_cache/
.pytest_cache/
.data/
secrets/
folders.yaml
docker-compose.override.yml
*.session
*.session-journal
.env
```

`LICENSE` (MIT):

```
MIT License

Copyright (c) 2026 Vital Kolas

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

`README.md` (stub; Task 14 completes it):

```markdown
# telegrief

Daily ranked digest of Telegram channel posts from your folders.
See `docs/superpowers/specs/2026-10-15-telegrief-design.md`.
```

`check.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

case "${1:-all}" in
    lint)
        uv run ruff format .
        uv run ruff check --fix .
        uv run mypy
        ;;
    test)
        uv run pytest "${@:2}"
        ;;
    all)
        uv run ruff format --check .
        uv run ruff check .
        uv run mypy
        uv run pytest
        ;;
    *)
        echo "usage: $0 [lint|test|all]"
        exit 1
        ;;
esac
```

`src/telegrief/__init__.py`:

```python
"""telegrief: daily ranked digest of Telegram channel posts from your folders."""

__version__ = "0.1.0"
```

`src/telegrief/py.typed`: empty file. `tests/__init__.py`: empty file.

- [ ] **Step 3: Write the smoke test**

`tests/test_smoke.py`:

```python
import telegrief


def test_version() -> None:
    assert telegrief.__version__ == "0.1.0"
```

- [ ] **Step 4: Sync and run checks**

Run: `chmod +x check.sh && uv sync && ./check.sh`
Expected: `uv.lock` created; ruff, mypy and pytest all pass (`1 passed`).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: project scaffold (uv, ruff, mypy, pytest)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Models and configuration

**Files:**
- Create: `src/telegrief/models.py`, `src/telegrief/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `ChannelInfo`, `RawPost`, `Post`, `ScoredPost`, `Digest`, `DigestItem`, enums `DigestStatus`, `ItemState`, `Delivery`; `Settings`, `FolderConfig`, `FoldersConfig`, `Scoring`, `load_folders(path) -> FoldersConfig`.

- [ ] **Step 1: Write models.py**

```python
"""Core data types shared by every module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


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
```

- [ ] **Step 2: Write the failing config tests**

`tests/test_config.py`:

```python
from pathlib import Path

import pytest
from pydantic import ValidationError

from telegrief.config import FolderConfig, FoldersConfig, Settings, load_folders

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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'telegrief.config'`

- [ ] **Step 4: Write config.py**

```python
"""Environment settings (pydantic-settings) and folders.yaml model."""

from __future__ import annotations

import re
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


class FoldersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folders: list[FolderConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names(self) -> FoldersConfig:
        names = [f.name for f in self.folders]
        if len(names) != len(set(names)):
            raise ValueError("folder names must be unique")
        return self

    def get(self, name: str) -> FolderConfig | None:
        return next((f for f in self.folders if f.name == name), None)


def load_folders(path: Path) -> FoldersConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return FoldersConfig.model_validate(data)
```

- [ ] **Step 5: Run tests and checks**

Run: `./check.sh`
Expected: all config tests PASS, ruff and mypy clean.

- [ ] **Step 6: Commit**

```bash
git add src/telegrief/models.py src/telegrief/config.py tests/test_config.py
git commit -m "feat: core models and configuration

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Scoring

**Files:**
- Create: `src/telegrief/scoring.py`
- Test: `tests/test_scoring.py`

**Interfaces:**
- Consumes: `Post`, `ChannelInfo`, `ScoredPost` (Task 2), `FolderConfig`, `Scoring` (Task 2).
- Produces: `engagement(post, channel) -> int`, `rank(posts, channels, cfg) -> list[ScoredPost]`, `format_count(n) -> str`, `format_score(score, scoring) -> str`, `format_engagement(e, channel) -> str`, `STRATEGIES`.

- [ ] **Step 1: Write the failing tests**

`tests/test_scoring.py`:

```python
from datetime import UTC, datetime

from telegrief.config import FolderConfig
from telegrief.models import ChannelInfo, Post
from telegrief.scoring import (
    engagement,
    format_count,
    format_engagement,
    format_score,
    rank,
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
    cfg = FolderConfig(name="x", scoring="per_view", min_denominator=200)
    [scored] = rank([post(views=50, reactions=10)], {-1001: CH}, cfg)
    assert scored.score == 10 / 200


def test_per_view_without_floor() -> None:
    cfg = FolderConfig(name="x", scoring="per_view", min_denominator=200)
    [scored] = rank([post(views=1000, reactions=41)], {-1001: CH}, cfg)
    assert scored.score == 0.041


def test_per_subscriber() -> None:
    cfg = FolderConfig(name="x", scoring="per_subscriber", min_denominator=200)
    [scored] = rank([post(reactions=50)], {-1001: CH}, cfg)
    assert scored.score == 5.0  # 50 / 10_000 * 1000


def test_raw() -> None:
    cfg = FolderConfig(name="x", scoring="raw")
    [scored] = rank([post(reactions=50)], {-1001: CH}, cfg)
    assert scored.score == 50.0


def test_rank_drops_zero_engagement() -> None:
    cfg = FolderConfig(name="x")
    posts = [post(1, reactions=0), post(2, reactions=1)]
    assert [s.post.msg_id for s in rank(posts, {-1001: CH}, cfg)] == [2]


def test_rank_drops_posts_from_unknown_channels() -> None:
    cfg = FolderConfig(name="x")
    assert rank([post(1, channel_id=-1099)], {-1001: CH}, cfg) == []


def test_rank_orders_by_score_then_engagement_then_date() -> None:
    cfg = FolderConfig(name="x", scoring="per_view", min_denominator=1)
    later = T0.replace(hour=13)
    posts = [
        post(1, views=1000, reactions=10),  # 0.01
        post(2, views=100, reactions=10),  # 0.10, engagement 10
        post(3, views=200, reactions=20, date=later),  # 0.10, engagement 20, later
        post(4, views=200, reactions=20),  # 0.10, engagement 20, earlier
    ]
    assert [s.post.msg_id for s in rank(posts, {-1001: CH}, cfg)] == [4, 3, 2, 1]


def test_rank_truncates_to_max_items() -> None:
    cfg = FolderConfig(name="x", max_items=2)
    posts = [post(i, reactions=i) for i in range(1, 6)]
    assert [s.post.msg_id for s in rank(posts, {-1001: CH}, cfg)] == [5, 4]


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_scoring.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'telegrief.scoring'`

- [ ] **Step 3: Write scoring.py**

```python
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
```

- [ ] **Step 4: Run tests and checks**

Run: `./check.sh`
Expected: all scoring tests PASS, ruff and mypy clean.

- [ ] **Step 5: Commit**

```bash
git add src/telegrief/scoring.py tests/test_scoring.py
git commit -m "feat: scoring strategies and ranking

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Storage and initial migration

**Files:**
- Create: `src/telegrief/migrations/001_init.sql`, `src/telegrief/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: models (Task 2), `ScoredPost` (Task 2).
- Produces: `Storage` with `open(path)`, `close()`, `migrate() -> int`, `upsert_channel(info, now)`, `get_channel(id) -> tuple[ChannelInfo, datetime] | None`, `upsert_posts(posts, now)`, `count_posts(channel_id) -> int`, `get_target(folder) -> int | None`, `set_target(folder, channel_id, now)`, `delete_target(folder) -> bool`, `create_digest(folder, target, window_start, window_end, scored, now) -> Digest`, `get_digest(id)`, `open_digest_for_target(target)`, `open_digests()`, `latest_digest(folder)`, `update_digest(digest)`, `pending_items(digest_id, limit) -> list[DigestItem]`, `count_items(digest_id, state) -> int`, `max_sent_msg_id(digest_id) -> int | None`, `update_item(item)`.

- [ ] **Step 1: Write the migration**

`src/telegrief/migrations/001_init.sql`:

```sql
CREATE TABLE channels (
    id INTEGER PRIMARY KEY,
    username TEXT,
    title TEXT NOT NULL,
    subscribers INTEGER NOT NULL DEFAULT 0,
    reactions_enabled INTEGER NOT NULL DEFAULT 1,
    noforwards INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE targets (
    folder TEXT PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE posts (
    channel_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    grouped_id INTEGER,
    date TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0,
    forwards INTEGER NOT NULL DEFAULT 0,
    reactions INTEGER NOT NULL DEFAULT 0,
    replies INTEGER NOT NULL DEFAULT 0,
    preview TEXT NOT NULL DEFAULT '',
    noforwards INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, msg_id)
);

CREATE TABLE digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder TEXT NOT NULL,
    target INTEGER NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    total INTEGER NOT NULL,
    title_msg_id INTEGER,
    footer_msg_id INTEGER,
    footer_text TEXT,
    status TEXT NOT NULL,
    last_batch_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX digests_target_status ON digests (target, status);

CREATE TABLE digest_items (
    digest_id INTEGER NOT NULL REFERENCES digests (id),
    rank INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    msg_ids TEXT NOT NULL,
    engagement INTEGER NOT NULL,
    views INTEGER NOT NULL,
    score REAL NOT NULL,
    noforwards INTEGER NOT NULL DEFAULT 0,
    preview TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    delivery TEXT,
    sent_msg_id INTEGER,
    sent_at TEXT,
    PRIMARY KEY (digest_id, rank)
);
```

- [ ] **Step 2: Write the failing tests**

`tests/test_storage.py`:

```python
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegrief.models import (
    ChannelInfo,
    Delivery,
    DigestStatus,
    ItemState,
    Post,
    RawPost,
    ScoredPost,
)
from telegrief.storage import Storage

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
CH = ChannelInfo(
    id=-1001, username="a", title="A", subscribers=10, reactions_enabled=True, noforwards=False
)


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = await Storage.open(tmp_path / "t.db")
    yield s
    await s.close()


def scored(msg_id: int, score: float, channel: ChannelInfo = CH) -> ScoredPost:
    post = Post(
        channel_id=channel.id,
        msg_ids=(msg_id, msg_id + 1),
        date=NOW - timedelta(hours=30),
        views=100,
        forwards=0,
        reactions=5,
        replies=0,
        preview="hi",
        noforwards=False,
    )
    return ScoredPost(post, channel, 5, score)


async def test_migrate_is_idempotent(storage: Storage) -> None:
    assert await storage.migrate() == 1
    assert await storage.migrate() == 1


async def test_channel_roundtrip(storage: Storage) -> None:
    assert await storage.get_channel(CH.id) is None
    await storage.upsert_channel(CH, NOW)
    got = await storage.get_channel(CH.id)
    assert got == (CH, NOW)
    newer = ChannelInfo(
        id=CH.id, username=None, title="A2", subscribers=11, reactions_enabled=False, noforwards=True
    )
    await storage.upsert_channel(newer, NOW + timedelta(hours=1))
    assert await storage.get_channel(CH.id) == (newer, NOW + timedelta(hours=1))


async def test_posts_upsert_and_count(storage: Storage) -> None:
    raw = RawPost(
        channel_id=CH.id,
        msg_id=1,
        grouped_id=None,
        date=NOW,
        views=1,
        forwards=0,
        reactions=0,
        replies=0,
        preview="",
        noforwards=False,
    )
    await storage.upsert_posts([raw, raw], NOW)
    await storage.upsert_posts([raw], NOW)
    assert await storage.count_posts(CH.id) == 1


async def test_target_roundtrip(storage: Storage) -> None:
    assert await storage.get_target("Memes") is None
    await storage.set_target("Memes", -100500, NOW)
    assert await storage.get_target("Memes") == -100500
    assert await storage.delete_target("Memes") is True
    assert await storage.delete_target("Memes") is False
    assert await storage.get_target("Memes") is None


async def test_create_digest_stores_items_in_rank_order(storage: Storage) -> None:
    start, end = NOW - timedelta(hours=48), NOW - timedelta(hours=24)
    digest = await storage.create_digest(
        "Memes", -100500, start, end, [scored(10, 0.5), scored(20, 0.2)], NOW
    )
    assert digest.id == 1
    assert digest.total == 2
    assert digest.status == DigestStatus.OPEN
    assert digest.window_start == start and digest.window_end == end
    items = await storage.pending_items(digest.id, limit=10)
    assert [(i.rank, i.msg_ids, i.score) for i in items] == [
        (1, (10, 11), 0.5),
        (2, (20, 21), 0.2),
    ]
    assert items[0].channel_id == CH.id
    assert items[0].preview == "hi"
    assert items[0].engagement == 5 and items[0].views == 100
    assert await storage.get_digest(digest.id) == digest
    assert await storage.get_digest(999) is None


async def test_create_digest_closes_older_open_digest_for_same_target(storage: Storage) -> None:
    start, end = NOW - timedelta(hours=48), NOW - timedelta(hours=24)
    first = await storage.create_digest("Memes", -100500, start, end, [scored(1, 1.0)], NOW)
    other = await storage.create_digest("News", -100600, start, end, [scored(1, 1.0)], NOW)
    second = await storage.create_digest("Memes", -100500, start, end, [scored(2, 1.0)], NOW)
    first_now = await storage.get_digest(first.id)
    assert first_now is not None and first_now.status == DigestStatus.DONE
    open_memes = await storage.open_digest_for_target(-100500)
    assert open_memes is not None and open_memes.id == second.id
    assert {d.id for d in await storage.open_digests()} == {other.id, second.id}
    latest = await storage.latest_digest("Memes")
    assert latest is not None and latest.id == second.id
    assert await storage.latest_digest("Nope") is None


async def test_pending_items_limit_and_item_updates(storage: Storage) -> None:
    start, end = NOW - timedelta(hours=48), NOW - timedelta(hours=24)
    digest = await storage.create_digest(
        "Memes", -100500, start, end, [scored(i, 1.0 / i) for i in range(1, 6)], NOW
    )
    batch = await storage.pending_items(digest.id, limit=2)
    assert [i.rank for i in batch] == [1, 2]
    assert await storage.max_sent_msg_id(digest.id) is None
    batch[0].state = ItemState.SENT
    batch[0].delivery = Delivery.FORWARD
    batch[0].sent_msg_id = 77
    batch[0].sent_at = NOW
    await storage.update_item(batch[0])
    batch[1].state = ItemState.FAILED
    await storage.update_item(batch[1])
    assert [i.rank for i in await storage.pending_items(digest.id, limit=10)] == [3, 4, 5]
    assert await storage.count_items(digest.id, ItemState.SENT) == 1
    assert await storage.count_items(digest.id, ItemState.FAILED) == 1
    assert await storage.count_items(digest.id, ItemState.PENDING) == 3
    assert await storage.max_sent_msg_id(digest.id) == 77


async def test_update_digest(storage: Storage) -> None:
    start, end = NOW - timedelta(hours=48), NOW - timedelta(hours=24)
    digest = await storage.create_digest("Memes", -100500, start, end, [scored(1, 1.0)], NOW)
    digest.title_msg_id = 5
    digest.footer_msg_id = 9
    digest.footer_text = "footer"
    digest.last_batch_at = NOW
    digest.status = DigestStatus.DONE
    await storage.update_digest(digest)
    assert await storage.get_digest(digest.id) == digest
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_storage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'telegrief.storage'`

- [ ] **Step 4: Write storage.py**

```python
"""SQLite persistence (aiosqlite, WAL) with plain-SQL migrations."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .models import (
    ChannelInfo,
    Delivery,
    Digest,
    DigestItem,
    DigestStatus,
    ItemState,
    RawPost,
    ScoredPost,
)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _req_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _channel_from_row(row: aiosqlite.Row) -> ChannelInfo:
    return ChannelInfo(
        id=row["id"],
        username=row["username"],
        title=row["title"],
        subscribers=row["subscribers"],
        reactions_enabled=bool(row["reactions_enabled"]),
        noforwards=bool(row["noforwards"]),
    )


def _digest_from_row(row: aiosqlite.Row) -> Digest:
    return Digest(
        id=row["id"],
        folder=row["folder"],
        target=row["target"],
        window_start=_req_dt(row["window_start"]),
        window_end=_req_dt(row["window_end"]),
        total=row["total"],
        status=DigestStatus(row["status"]),
        created_at=_req_dt(row["created_at"]),
        title_msg_id=row["title_msg_id"],
        footer_msg_id=row["footer_msg_id"],
        footer_text=row["footer_text"],
        last_batch_at=_dt(row["last_batch_at"]),
    )


def _item_from_row(row: aiosqlite.Row) -> DigestItem:
    return DigestItem(
        digest_id=row["digest_id"],
        rank=row["rank"],
        channel_id=row["channel_id"],
        msg_ids=tuple(json.loads(row["msg_ids"])),
        engagement=row["engagement"],
        views=row["views"],
        score=row["score"],
        noforwards=bool(row["noforwards"]),
        preview=row["preview"],
        state=ItemState(row["state"]),
        delivery=Delivery(row["delivery"]) if row["delivery"] else None,
        sent_msg_id=row["sent_msg_id"],
        sent_at=_dt(row["sent_at"]),
    )


class Storage:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    async def open(cls, path: Path | str) -> Storage:
        conn = await aiosqlite.connect(str(path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        storage = cls(conn)
        await storage.migrate()
        return storage

    async def close(self) -> None:
        await self._conn.close()

    async def migrate(self) -> int:
        await self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        row = await self._fetchone("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version")
        current = int(row["v"]) if row is not None else 0
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            number = int(path.name.split("_", 1)[0])
            if number <= current:
                continue
            await self._conn.executescript(path.read_text(encoding="utf-8"))
            await self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (number,))
            await self._conn.commit()
            current = number
        return current

    async def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        cur = await self._conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        cur = await self._conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)

    # ---- channels ----------------------------------------------------------

    async def upsert_channel(self, info: ChannelInfo, now: datetime) -> None:
        await self._conn.execute(
            """
            INSERT INTO channels
                (id, username, title, subscribers, reactions_enabled, noforwards, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username = excluded.username,
                title = excluded.title,
                subscribers = excluded.subscribers,
                reactions_enabled = excluded.reactions_enabled,
                noforwards = excluded.noforwards,
                updated_at = excluded.updated_at
            """,
            (
                info.id,
                info.username,
                info.title,
                info.subscribers,
                int(info.reactions_enabled),
                int(info.noforwards),
                now.isoformat(),
            ),
        )
        await self._conn.commit()

    async def get_channel(self, channel_id: int) -> tuple[ChannelInfo, datetime] | None:
        row = await self._fetchone("SELECT * FROM channels WHERE id = ?", (channel_id,))
        if row is None:
            return None
        return _channel_from_row(row), _req_dt(row["updated_at"])

    # ---- posts ---------------------------------------------------------------

    async def upsert_posts(self, posts: Iterable[RawPost], now: datetime) -> None:
        await self._conn.executemany(
            """
            INSERT INTO posts
                (channel_id, msg_id, grouped_id, date, views, forwards, reactions, replies,
                 preview, noforwards, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, msg_id) DO UPDATE SET
                views = excluded.views,
                forwards = excluded.forwards,
                reactions = excluded.reactions,
                replies = excluded.replies,
                preview = excluded.preview,
                noforwards = excluded.noforwards,
                fetched_at = excluded.fetched_at
            """,
            [
                (
                    p.channel_id,
                    p.msg_id,
                    p.grouped_id,
                    p.date.isoformat(),
                    p.views,
                    p.forwards,
                    p.reactions,
                    p.replies,
                    p.preview,
                    int(p.noforwards),
                    now.isoformat(),
                )
                for p in posts
            ],
        )
        await self._conn.commit()

    async def count_posts(self, channel_id: int) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM posts WHERE channel_id = ?", (channel_id,)
        )
        return int(row["n"]) if row is not None else 0

    # ---- targets -------------------------------------------------------------

    async def get_target(self, folder: str) -> int | None:
        row = await self._fetchone("SELECT channel_id FROM targets WHERE folder = ?", (folder,))
        return int(row["channel_id"]) if row is not None else None

    async def set_target(self, folder: str, channel_id: int, now: datetime) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO targets (folder, channel_id, created_at) VALUES (?, ?, ?)",
            (folder, channel_id, now.isoformat()),
        )
        await self._conn.commit()

    async def delete_target(self, folder: str) -> bool:
        cur = await self._conn.execute("DELETE FROM targets WHERE folder = ?", (folder,))
        await self._conn.commit()
        return cur.rowcount > 0

    # ---- digests -------------------------------------------------------------

    async def create_digest(
        self,
        folder: str,
        target: int,
        window_start: datetime,
        window_end: datetime,
        scored: list[ScoredPost],
        now: datetime,
    ) -> Digest:
        await self._conn.execute(
            "UPDATE digests SET status = ? WHERE target = ? AND status = ?",
            (DigestStatus.DONE, target, DigestStatus.OPEN),
        )
        cur = await self._conn.execute(
            """
            INSERT INTO digests
                (folder, target, window_start, window_end, total, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                folder,
                target,
                window_start.isoformat(),
                window_end.isoformat(),
                len(scored),
                DigestStatus.OPEN,
                now.isoformat(),
            ),
        )
        digest_id = cur.lastrowid
        assert digest_id is not None
        await self._conn.executemany(
            """
            INSERT INTO digest_items
                (digest_id, rank, channel_id, msg_id, msg_ids, engagement, views, score,
                 noforwards, preview, state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    digest_id,
                    rank,
                    s.post.channel_id,
                    s.post.msg_id,
                    json.dumps(list(s.post.msg_ids)),
                    s.engagement,
                    s.post.views,
                    s.score,
                    int(s.post.noforwards),
                    s.post.preview,
                    ItemState.PENDING,
                )
                for rank, s in enumerate(scored, start=1)
            ],
        )
        await self._conn.commit()
        return Digest(
            id=digest_id,
            folder=folder,
            target=target,
            window_start=window_start,
            window_end=window_end,
            total=len(scored),
            status=DigestStatus.OPEN,
            created_at=now,
        )

    async def get_digest(self, digest_id: int) -> Digest | None:
        row = await self._fetchone("SELECT * FROM digests WHERE id = ?", (digest_id,))
        return _digest_from_row(row) if row is not None else None

    async def open_digest_for_target(self, target: int) -> Digest | None:
        row = await self._fetchone(
            "SELECT * FROM digests WHERE target = ? AND status = ? ORDER BY id DESC LIMIT 1",
            (target, DigestStatus.OPEN),
        )
        return _digest_from_row(row) if row is not None else None

    async def open_digests(self) -> list[Digest]:
        rows = await self._fetchall(
            "SELECT * FROM digests WHERE status = ? ORDER BY id", (DigestStatus.OPEN,)
        )
        return [_digest_from_row(r) for r in rows]

    async def latest_digest(self, folder: str) -> Digest | None:
        row = await self._fetchone(
            "SELECT * FROM digests WHERE folder = ? ORDER BY id DESC LIMIT 1", (folder,)
        )
        return _digest_from_row(row) if row is not None else None

    async def update_digest(self, digest: Digest) -> None:
        await self._conn.execute(
            """
            UPDATE digests SET title_msg_id = ?, footer_msg_id = ?, footer_text = ?,
                status = ?, last_batch_at = ?
            WHERE id = ?
            """,
            (
                digest.title_msg_id,
                digest.footer_msg_id,
                digest.footer_text,
                digest.status,
                _iso(digest.last_batch_at),
                digest.id,
            ),
        )
        await self._conn.commit()

    # ---- items ---------------------------------------------------------------

    async def pending_items(self, digest_id: int, limit: int) -> list[DigestItem]:
        rows = await self._fetchall(
            """
            SELECT * FROM digest_items WHERE digest_id = ? AND state = ?
            ORDER BY rank LIMIT ?
            """,
            (digest_id, ItemState.PENDING, limit),
        )
        return [_item_from_row(r) for r in rows]

    async def count_items(self, digest_id: int, state: ItemState) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM digest_items WHERE digest_id = ? AND state = ?",
            (digest_id, state),
        )
        return int(row["n"]) if row is not None else 0

    async def max_sent_msg_id(self, digest_id: int) -> int | None:
        row = await self._fetchone(
            "SELECT MAX(sent_msg_id) AS m FROM digest_items WHERE digest_id = ? AND state = ?",
            (digest_id, ItemState.SENT),
        )
        return int(row["m"]) if row is not None and row["m"] is not None else None

    async def update_item(self, item: DigestItem) -> None:
        await self._conn.execute(
            """
            UPDATE digest_items SET state = ?, delivery = ?, sent_msg_id = ?, sent_at = ?
            WHERE digest_id = ? AND rank = ?
            """,
            (
                item.state,
                item.delivery,
                item.sent_msg_id,
                _iso(item.sent_at),
                item.digest_id,
                item.rank,
            ),
        )
        await self._conn.commit()
```

- [ ] **Step 5: Run tests and checks**

Run: `./check.sh`
Expected: all storage tests PASS, ruff and mypy clean. If mypy complains about `cur.rowcount`, it is an `int` in aiosqlite 0.22; if it complains about `cur.lastrowid`, keep the `assert`.

- [ ] **Step 6: Commit**

```bash
git add src/telegrief/migrations src/telegrief/storage.py tests/test_storage.py
git commit -m "feat: sqlite storage with migrations

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Gateway protocols and test fakes

**Files:**
- Create: `src/telegrief/tg/__init__.py`, `src/telegrief/tg/protocols.py`, `tests/fakes.py`
- Test: `tests/test_fakes.py`

**Interfaces:**
- Consumes: `ChannelInfo`, `RawPost` (Task 2).
- Produces: exceptions `GatewayError`, `FolderNotFound`, `ChannelUnavailable`, `ForwardRestricted`, `MediaTooLarge`, `CopyFailed`, `TargetUnavailable`; type aliases `ReadHandler`, `NextHandler`, `CommandHandlerFn`; Protocols `UserGateway`, `BotGateway`; test doubles `FakeUser`, `FakeBot` with the exact method signatures below. Every later task's tests use these fakes.

- [ ] **Step 1: Write protocols.py**

`src/telegrief/tg/__init__.py`: empty file.

`src/telegrief/tg/protocols.py`:

```python
"""What the application needs from Telegram, independent of Telethon."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..models import ChannelInfo, RawPost


class GatewayError(Exception):
    """Base class for Telegram-side failures the application handles."""


class FolderNotFound(GatewayError):
    """No dialog filter with the configured title."""


class ChannelUnavailable(GatewayError):
    """Channel is private, deleted, or the account left it."""


class ForwardRestricted(GatewayError):
    """Content protection forbids forwarding."""


class MediaTooLarge(GatewayError):
    """Media exceeds MAX_COPY_MB."""


class CopyFailed(GatewayError):
    """Download or re-upload of protected content failed."""


class TargetUnavailable(GatewayError):
    """Digest channel is gone or the bot lacks rights there."""


ReadHandler = Callable[[int, int], Awaitable[None]]
NextHandler = Callable[[int], Awaitable[None]]
CommandHandlerFn = Callable[[str, list[str]], Awaitable[str]]


@runtime_checkable
class UserGateway(Protocol):
    @property
    def me_id(self) -> int: ...

    async def folder_channels(self, folder_name: str) -> list[int]: ...

    async def channel_info(self, channel_id: int) -> ChannelInfo: ...

    def iter_posts(
        self, channel_id: int, start: datetime, end: datetime
    ) -> AsyncIterator[RawPost]: ...

    async def create_digest_channel(self, title: str, bot_username: str) -> int: ...

    async def forward_to_relay(self, channel_id: int, msg_ids: list[int]) -> list[int]: ...

    async def copy_to_relay(
        self, channel_id: int, msg_ids: list[int], caption: str, max_bytes: int
    ) -> list[int]: ...

    async def read_marker(self, channel_id: int) -> int: ...

    def on_channel_read(self, handler: ReadHandler) -> None: ...

    async def ping_bot(self, bot_username: str) -> None: ...


@runtime_checkable
class BotGateway(Protocol):
    @property
    def username(self) -> str: ...

    async def forward_from_relay(self, target: int, relay_ids: list[int]) -> list[int]: ...

    async def resend_from_relay(
        self, target: int, relay_ids: list[int], caption: str
    ) -> list[int]: ...

    async def delete_relay(self, relay_ids: list[int]) -> None: ...

    async def send_text(
        self,
        target: int,
        text: str,
        *,
        next_digest_id: int | None = None,
        next_label: str = "Next",
        preview: bool = False,
    ) -> int: ...

    async def edit_text(
        self,
        target: int,
        msg_id: int,
        text: str,
        *,
        next_digest_id: int | None = None,
        next_label: str = "Next",
    ) -> None: ...

    async def send_owner(self, text: str) -> None: ...

    def on_next_button(self, handler: NextHandler) -> None: ...

    def on_owner_command(self, handler: CommandHandlerFn) -> None: ...
```

- [ ] **Step 2: Write the fakes**

`tests/fakes.py`:

```python
"""In-memory stand-ins for the Telegram gateways used by unit tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

from telegrief.models import ChannelInfo, RawPost
from telegrief.tg.protocols import (
    ChannelUnavailable,
    CommandHandlerFn,
    CopyFailed,
    FolderNotFound,
    ForwardRestricted,
    MediaTooLarge,
    NextHandler,
    ReadHandler,
    TargetUnavailable,
)


class FakeUser:
    def __init__(self) -> None:
        self.me_id = 1
        self.folders: dict[str, list[int]] = {}
        self.channels: dict[int, ChannelInfo] = {}
        self.posts: dict[int, list[RawPost]] = {}
        self.read_markers: dict[int, int] = {}
        self.media_bytes: dict[tuple[int, int], int] = {}
        self.restricted: set[int] = set()  # forward_to_relay raises ForwardRestricted
        self.copy_fails: set[int] = set()  # copy_to_relay raises CopyFailed
        self.unavailable: set[int] = set()  # channel_info raises ChannelUnavailable
        self.forwarded: list[tuple[int, list[int]]] = []
        self.copied: list[tuple[int, list[int], str]] = []
        self.created: list[tuple[str, str]] = []
        self.pinged: list[str] = []
        self.channel_info_calls = 0
        self._next_relay_id = 1000
        self._next_channel_id = -1009000000000
        self._read_handler: ReadHandler | None = None

    async def folder_channels(self, folder_name: str) -> list[int]:
        if folder_name not in self.folders:
            raise FolderNotFound(folder_name)
        return list(self.folders[folder_name])

    async def channel_info(self, channel_id: int) -> ChannelInfo:
        self.channel_info_calls += 1
        if channel_id in self.unavailable or channel_id not in self.channels:
            raise ChannelUnavailable(str(channel_id))
        return self.channels[channel_id]

    async def iter_posts(
        self, channel_id: int, start: datetime, end: datetime
    ) -> AsyncIterator[RawPost]:
        for post in self.posts.get(channel_id, []):
            yield post

    async def create_digest_channel(self, title: str, bot_username: str) -> int:
        self._next_channel_id -= 1
        self.created.append((title, bot_username))
        return self._next_channel_id

    def _relay_ids(self, n: int) -> list[int]:
        ids = list(range(self._next_relay_id, self._next_relay_id + n))
        self._next_relay_id += n
        return ids

    async def forward_to_relay(self, channel_id: int, msg_ids: list[int]) -> list[int]:
        if channel_id in self.restricted:
            raise ForwardRestricted(str(channel_id))
        self.forwarded.append((channel_id, list(msg_ids)))
        return self._relay_ids(len(msg_ids))

    async def copy_to_relay(
        self, channel_id: int, msg_ids: list[int], caption: str, max_bytes: int
    ) -> list[int]:
        size = sum(self.media_bytes.get((channel_id, m), 0) for m in msg_ids)
        if size > max_bytes:
            raise MediaTooLarge(f"{size} > {max_bytes}")
        if channel_id in self.copy_fails:
            raise CopyFailed(str(channel_id))
        self.copied.append((channel_id, list(msg_ids), caption))
        return self._relay_ids(len(msg_ids))

    async def read_marker(self, channel_id: int) -> int:
        return self.read_markers.get(channel_id, 0)

    def on_channel_read(self, handler: ReadHandler) -> None:
        self._read_handler = handler

    async def ping_bot(self, bot_username: str) -> None:
        self.pinged.append(bot_username)

    async def emit_read(self, channel_id: int, max_id: int) -> None:
        """Simulate Telegram reporting that the owner read up to max_id."""
        assert self._read_handler is not None, "on_channel_read not bound"
        self.read_markers[channel_id] = max_id
        await self._read_handler(channel_id, max_id)


class FakeBot:
    def __init__(self) -> None:
        self.username = "telegrief_bot"
        # target -> [(msg_id, text, next_digest_id)]
        self.messages: dict[int, list[tuple[int, str, int | None]]] = {}
        self.edits: list[tuple[int, int, str, int | None]] = []
        self.forwards: list[tuple[int, list[int]]] = []
        self.resent: list[tuple[int, list[int], str]] = []
        self.deleted: list[list[int]] = []
        self.owner_messages: list[str] = []
        self.bad_targets: set[int] = set()
        self._next_msg_id: dict[int, int] = {}
        self._next_handler: NextHandler | None = None
        self._command_handler: CommandHandlerFn | None = None

    def _ids(self, target: int, n: int) -> list[int]:
        start = self._next_msg_id.get(target, 1)
        self._next_msg_id[target] = start + n
        return list(range(start, start + n))

    def _check(self, target: int) -> None:
        if target in self.bad_targets:
            raise TargetUnavailable(str(target))

    async def forward_from_relay(self, target: int, relay_ids: list[int]) -> list[int]:
        self._check(target)
        self.forwards.append((target, list(relay_ids)))
        return self._ids(target, len(relay_ids))

    async def resend_from_relay(
        self, target: int, relay_ids: list[int], caption: str
    ) -> list[int]:
        self._check(target)
        self.resent.append((target, list(relay_ids), caption))
        return self._ids(target, 1)

    async def delete_relay(self, relay_ids: list[int]) -> None:
        self.deleted.append(list(relay_ids))

    async def send_text(
        self,
        target: int,
        text: str,
        *,
        next_digest_id: int | None = None,
        next_label: str = "Next",
        preview: bool = False,
    ) -> int:
        self._check(target)
        (msg_id,) = self._ids(target, 1)
        self.messages.setdefault(target, []).append((msg_id, text, next_digest_id))
        return msg_id

    async def edit_text(
        self,
        target: int,
        msg_id: int,
        text: str,
        *,
        next_digest_id: int | None = None,
        next_label: str = "Next",
    ) -> None:
        self.edits.append((target, msg_id, text, next_digest_id))

    async def send_owner(self, text: str) -> None:
        self.owner_messages.append(text)

    def on_next_button(self, handler: NextHandler) -> None:
        self._next_handler = handler

    def on_owner_command(self, handler: CommandHandlerFn) -> None:
        self._command_handler = handler

    async def press_next(self, digest_id: int) -> None:
        assert self._next_handler is not None, "on_next_button not bound"
        await self._next_handler(digest_id)

    async def command(self, cmd: str, args: list[str]) -> str:
        assert self._command_handler is not None, "on_owner_command not bound"
        return await self._command_handler(cmd, args)

    def texts(self, target: int) -> list[str]:
        return [text for _, text, _ in self.messages.get(target, [])]

    def last(self, target: int) -> tuple[int, str, int | None]:
        return self.messages[target][-1]
```

- [ ] **Step 3: Write the protocol conformance test**

`tests/test_fakes.py`:

```python
from telegrief.tg.protocols import BotGateway, UserGateway
from tests.fakes import FakeBot, FakeUser


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeUser(), UserGateway)
    assert isinstance(FakeBot(), BotGateway)
```

- [ ] **Step 4: Run tests and checks**

Run: `./check.sh`
Expected: PASS. mypy only checks `src/`, so fakes are checked by ruff only; keep them annotated anyway.

- [ ] **Step 5: Commit**

```bash
git add src/telegrief/tg tests/fakes.py tests/test_fakes.py
git commit -m "feat: gateway protocols and in-memory fakes

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Collector

**Files:**
- Create: `src/telegrief/collector.py`
- Test: `tests/test_collector.py`

**Interfaces:**
- Consumes: `UserGateway`, `ChannelUnavailable` (Task 5), `Storage` (Task 4), models (Task 2).
- Produces: `window(now, from_h, to_h) -> tuple[datetime, datetime]`, `group_albums(raw) -> list[Post]`, `Collected(channels, posts, errors)`, `Collector(user, storage, cache_ttl=timedelta(hours=24))` with `channel_info(channel_id, now) -> ChannelInfo` and `collect(folder, start, end, now) -> Collected`.

- [ ] **Step 1: Write the failing tests**

`tests/test_collector.py`:

```python
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegrief.collector import Collector, group_albums, window
from telegrief.models import ChannelInfo, RawPost
from telegrief.storage import Storage
from telegrief.tg.protocols import FolderNotFound
from tests.fakes import FakeUser

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
START, END = NOW - timedelta(hours=48), NOW - timedelta(hours=24)
CH_ID = -1001
CH = ChannelInfo(
    id=CH_ID, username="a", title="A", subscribers=100, reactions_enabled=True, noforwards=False
)


def raw(
    msg_id: int,
    *,
    channel_id: int = CH_ID,
    grouped_id: int | None = None,
    date: datetime = START + timedelta(hours=1),
    views: int = 100,
    forwards: int = 0,
    reactions: int = 5,
    preview: str = "",
    noforwards: bool = False,
) -> RawPost:
    return RawPost(
        channel_id=channel_id,
        msg_id=msg_id,
        grouped_id=grouped_id,
        date=date,
        views=views,
        forwards=forwards,
        reactions=reactions,
        replies=0,
        preview=preview,
        noforwards=noforwards,
    )


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = await Storage.open(tmp_path / "t.db")
    yield s
    await s.close()


def test_window() -> None:
    assert window(NOW, 48, 24) == (START, END)
    assert window(NOW, 24, 0) == (NOW - timedelta(hours=24), NOW)


def test_group_albums_aggregates_group() -> None:
    t = START + timedelta(hours=1)
    posts = group_albums(
        [
            raw(12, grouped_id=7, date=t + timedelta(seconds=2), views=300, reactions=0),
            raw(10, grouped_id=7, date=t, views=310, reactions=40, preview="cap"),
            raw(11, grouped_id=7, date=t + timedelta(seconds=1), views=305, reactions=2),
        ]
    )
    [album] = posts
    assert album.msg_ids == (10, 11, 12)
    assert album.reactions == 42
    assert album.views == 310
    assert album.date == t
    assert album.preview == "cap"


def test_group_albums_keeps_singletons_in_order() -> None:
    posts = group_albums([raw(3), raw(1, grouped_id=9), raw(2, grouped_id=9), raw(4)])
    assert [p.msg_ids for p in posts] == [(3,), (1, 2), (4,)]


def test_group_albums_noforwards_if_any_member() -> None:
    [album] = group_albums([raw(1, grouped_id=1), raw(2, grouped_id=1, noforwards=True)])
    assert album.noforwards is True


async def test_collect_filters_window_and_stores_posts(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = [CH_ID]
    user.channels[CH_ID] = CH
    user.posts[CH_ID] = [
        raw(1, date=START - timedelta(seconds=1)),
        raw(2, date=START),
        raw(3, date=END - timedelta(seconds=1)),
        raw(4, date=END),
    ]
    result = await Collector(user, storage).collect("Memes", START, END, NOW)
    assert [p.msg_id for p in result.posts] == [2, 3]
    assert result.channels == {CH_ID: CH}
    assert result.errors == []
    assert await storage.count_posts(CH_ID) == 2


async def test_collect_uses_channel_cache_within_ttl(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = [CH_ID]
    await storage.upsert_channel(CH, NOW - timedelta(hours=23))
    result = await Collector(user, storage).collect("Memes", START, END, NOW)
    assert user.channel_info_calls == 0
    assert result.channels[CH_ID] == CH


async def test_collect_refreshes_channel_after_ttl(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = [CH_ID]
    fresh = ChannelInfo(
        id=CH_ID, username="a", title="A", subscribers=200, reactions_enabled=True, noforwards=False
    )
    user.channels[CH_ID] = fresh
    await storage.upsert_channel(CH, NOW - timedelta(hours=25))
    result = await Collector(user, storage).collect("Memes", START, END, NOW)
    assert user.channel_info_calls == 1
    assert result.channels[CH_ID].subscribers == 200
    assert await storage.get_channel(CH_ID) == (fresh, NOW)


async def test_collect_skips_unavailable_channel_and_reports(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = [CH_ID, -1002]
    user.channels[CH_ID] = CH
    user.posts[CH_ID] = [raw(1)]
    result = await Collector(user, storage).collect("Memes", START, END, NOW)
    assert [p.msg_id for p in result.posts] == [1]
    assert result.errors == ["-1002: -1002"]


async def test_collect_marks_noforwards_from_channel(storage: Storage) -> None:
    user = FakeUser()
    user.folders["Memes"] = [CH_ID]
    user.channels[CH_ID] = ChannelInfo(
        id=CH_ID, username="a", title="A", subscribers=1, reactions_enabled=True, noforwards=True
    )
    user.posts[CH_ID] = [raw(1)]
    result = await Collector(user, storage).collect("Memes", START, END, NOW)
    assert result.posts[0].noforwards is True


async def test_collect_raises_for_missing_folder(storage: Storage) -> None:
    with pytest.raises(FolderNotFound):
        await Collector(FakeUser(), storage).collect("Nope", START, END, NOW)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_collector.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'telegrief.collector'`

- [ ] **Step 3: Write collector.py**

```python
"""Folder → channels → posts, with album grouping and a channel-info cache."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

from .models import ChannelInfo, Post, RawPost
from .tg.protocols import ChannelUnavailable, UserGateway

if TYPE_CHECKING:
    from .storage import Storage

CACHE_TTL = timedelta(hours=24)


def window(now: datetime, from_h: int, to_h: int) -> tuple[datetime, datetime]:
    """Half-open [now - from_h, now - to_h)."""
    return now - timedelta(hours=from_h), now - timedelta(hours=to_h)


def group_albums(raw: list[RawPost]) -> list[Post]:
    """Merge messages sharing grouped_id into one Post; keep first-seen order."""
    groups: dict[tuple[int, str], list[RawPost]] = {}
    for item in raw:
        key = (item.channel_id, f"g{item.grouped_id}" if item.grouped_id else f"m{item.msg_id}")
        groups.setdefault(key, []).append(item)
    posts: list[Post] = []
    for members in groups.values():
        members.sort(key=lambda m: m.msg_id)
        preview = next((m.preview for m in members if m.preview), "")
        posts.append(
            Post(
                channel_id=members[0].channel_id,
                msg_ids=tuple(m.msg_id for m in members),
                date=min(m.date for m in members),
                views=max(m.views for m in members),
                forwards=max(m.forwards for m in members),
                reactions=sum(m.reactions for m in members),
                replies=max(m.replies for m in members),
                preview=preview,
                noforwards=any(m.noforwards for m in members),
            )
        )
    return posts


@dataclass
class Collected:
    channels: dict[int, ChannelInfo] = field(default_factory=dict)
    posts: list[Post] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Collector:
    def __init__(self, user: UserGateway, storage: Storage, cache_ttl: timedelta = CACHE_TTL) -> None:
        self._user = user
        self._storage = storage
        self._ttl = cache_ttl

    async def channel_info(self, channel_id: int, now: datetime) -> ChannelInfo:
        cached = await self._storage.get_channel(channel_id)
        if cached is not None and now - cached[1] < self._ttl:
            return cached[0]
        info = await self._user.channel_info(channel_id)
        await self._storage.upsert_channel(info, now)
        return info

    async def collect(
        self, folder: str, start: datetime, end: datetime, now: datetime
    ) -> Collected:
        result = Collected()
        for channel_id in await self._user.folder_channels(folder):
            try:
                info = await self.channel_info(channel_id, now)
                raw = [
                    replace(p, noforwards=p.noforwards or info.noforwards)
                    async for p in self._user.iter_posts(channel_id, start, end)
                    if start <= p.date < end
                ]
            except ChannelUnavailable as exc:
                logger.warning("folder {}: skipping channel {}: {}", folder, channel_id, exc)
                result.errors.append(f"{channel_id}: {exc}")
                continue
            result.channels[channel_id] = info
            await self._storage.upsert_posts(raw, now)
            result.posts.extend(group_albums(raw))
        return result
```

- [ ] **Step 4: Run tests and checks**

Run: `./check.sh`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/telegrief/collector.py tests/test_collector.py
git commit -m "feat: collector with album grouping and channel cache

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Target resolver

**Files:**
- Create: `src/telegrief/targets.py`
- Test: `tests/test_targets.py`

**Interfaces:**
- Consumes: `UserGateway` (Task 5), `Storage` (Task 4), `FolderConfig` (Task 2).
- Produces: `TargetResolver(user, storage, bot_username)` with `resolve(cfg, now) -> int` and `reset(folder) -> bool`.

- [ ] **Step 1: Write the failing tests**

`tests/test_targets.py`:

```python
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegrief.config import FolderConfig
from telegrief.storage import Storage
from telegrief.targets import TargetResolver
from tests.fakes import FakeUser

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = await Storage.open(tmp_path / "t.db")
    yield s
    await s.close()


async def test_yaml_target_wins(storage: Storage) -> None:
    user = FakeUser()
    resolver = TargetResolver(user, storage, "telegrief_bot")
    assert await resolver.resolve(FolderConfig(name="Memes", target=-100777), NOW) == -100777
    assert user.created == []
    assert await storage.get_target("Memes") is None


async def test_creates_channel_once_and_stores_mapping(storage: Storage) -> None:
    user = FakeUser()
    resolver = TargetResolver(user, storage, "telegrief_bot")
    cfg = FolderConfig(name="Memes")
    first = await resolver.resolve(cfg, NOW)
    second = await resolver.resolve(cfg, NOW)
    assert first == second
    assert user.created == [("Memes · digest", "telegrief_bot")]
    assert await storage.get_target("Memes") == first


async def test_reset_clears_mapping(storage: Storage) -> None:
    user = FakeUser()
    resolver = TargetResolver(user, storage, "telegrief_bot")
    cfg = FolderConfig(name="Memes")
    first = await resolver.resolve(cfg, NOW)
    assert await resolver.reset("Memes") is True
    assert await resolver.reset("Memes") is False
    assert await resolver.resolve(cfg, NOW) != first
    assert len(user.created) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_targets.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'telegrief.targets'`

- [ ] **Step 3: Write targets.py**

```python
"""Resolve (or create) the digest channel for a folder."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from loguru import logger

from .config import FolderConfig
from .tg.protocols import UserGateway

if TYPE_CHECKING:
    from .storage import Storage


class TargetResolver:
    def __init__(self, user: UserGateway, storage: Storage, bot_username: str) -> None:
        self._user = user
        self._storage = storage
        self._bot_username = bot_username

    async def resolve(self, cfg: FolderConfig, now: datetime) -> int:
        if cfg.target is not None:
            return cfg.target
        existing = await self._storage.get_target(cfg.name)
        if existing is not None:
            return existing
        channel_id = await self._user.create_digest_channel(
            f"{cfg.name} · digest", self._bot_username
        )
        await self._storage.set_target(cfg.name, channel_id, now)
        logger.info("folder {}: created digest channel {}", cfg.name, channel_id)
        return channel_id

    async def reset(self, folder: str) -> bool:
        return await self._storage.delete_target(folder)
```

- [ ] **Step 4: Run tests and checks**

Run: `./check.sh`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/telegrief/targets.py tests/test_targets.py
git commit -m "feat: digest channel target resolver

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Delivery (forward / copy / link) and message rendering

**Files:**
- Create: `src/telegrief/delivery.py`
- Test: `tests/test_delivery.py`

**Interfaces:**
- Consumes: gateways + exceptions (Task 5), `DigestItem`, `ChannelInfo`, `Delivery` (Task 2), `FolderConfig` (Task 2), `format_*` (Task 3).
- Produces: `bare_id(channel_id) -> int`, `post_link(channel, msg_id) -> str`, `build_caption(channel, msg_id, preview) -> str`, `render_title(folder, day, total, n_channels) -> str`, `render_footer(batch, channels, cfg, shown, total, done) -> str`, `Deliverer(user, bot, max_copy_bytes)` with `deliver(target, item, channel) -> tuple[Delivery, int]` (returns delivery kind and the **last** message id posted in the target).

- [ ] **Step 1: Write the failing tests**

`tests/test_delivery.py`:

```python
from datetime import date

import pytest

from telegrief.config import FolderConfig
from telegrief.delivery import (
    CAPTION_LIMIT,
    Deliverer,
    bare_id,
    build_caption,
    post_link,
    render_footer,
    render_title,
)
from telegrief.models import ChannelInfo, Delivery, DigestItem
from tests.fakes import FakeBot, FakeUser

PUBLIC = ChannelInfo(
    id=-1001234, username="memes", title="Memes", subscribers=10, reactions_enabled=True,
    noforwards=False,
)
PRIVATE = ChannelInfo(
    id=-1005678, username=None, title="Secret <Club> & co", subscribers=10,
    reactions_enabled=False, noforwards=True,
)
TARGET = -100900


def item(
    rank: int = 1,
    *,
    channel: ChannelInfo = PUBLIC,
    msg_ids: tuple[int, ...] = (10,),
    noforwards: bool = False,
    preview: str = "lol",
) -> DigestItem:
    return DigestItem(
        digest_id=1,
        rank=rank,
        channel_id=channel.id,
        msg_ids=msg_ids,
        engagement=120,
        views=2900,
        score=0.041,
        noforwards=noforwards,
        preview=preview,
    )


def test_bare_id() -> None:
    assert bare_id(-1001234567890) == 1234567890
    assert bare_id(1234) == 1234


def test_post_link_public_and_private() -> None:
    assert post_link(PUBLIC, 10) == "https://t.me/memes/10"
    assert post_link(PRIVATE, 10) == "https://t.me/c/5678/10"


def test_build_caption_has_source_and_text() -> None:
    caption = build_caption(PUBLIC, 10, "hello")
    assert caption == "↗️ @memes · https://t.me/memes/10\n\nhello"
    assert build_caption(PUBLIC, 10, "") == "↗️ @memes · https://t.me/memes/10"


def test_build_caption_truncates() -> None:
    assert len(build_caption(PUBLIC, 10, "x" * 5000)) == CAPTION_LIMIT


def test_render_title() -> None:
    assert render_title("Memes", date(2026, 10, 14), 57, 23) == (
        "📂 Memes · 2026-10-14 · 57 posts from 23 channels"
    )
    assert render_title("Memes", date(2026, 10, 14), 0, 0) == (
        "📂 Memes · 2026-10-14 · no posts in window"
    )


def test_render_footer_lines_and_counter() -> None:
    cfg = FolderConfig(name="Memes")
    text = render_footer(
        [item(1), item(2, channel=PRIVATE)],
        {PUBLIC.id: PUBLIC, PRIVATE.id: PRIVATE},
        cfg,
        shown=10,
        total=57,
        done=False,
    )
    assert text.splitlines() == [
        "#1 · 4.1% · 120👍 / 2.9k👁 · @memes",
        "#2 · 4.1% · 120↗️ / 2.9k👁 · Secret &lt;Club&gt; &amp; co",
        "shown 10/57",
    ]


def test_render_footer_escapes_html() -> None:
    cfg = FolderConfig(name="Memes")
    text = render_footer([item(1, channel=PRIVATE)], {PRIVATE.id: PRIVATE}, cfg, 1, 1, True)
    assert "<Club>" not in text and "&lt;Club&gt;" in text


def test_render_footer_done_lines() -> None:
    cfg = FolderConfig(name="Memes")
    full = render_footer([item(1)], {PUBLIC.id: PUBLIC}, cfg, shown=57, total=57, done=True)
    assert full.splitlines()[-1] == "✅ all 57 shown"
    partial = render_footer([item(1)], {PUBLIC.id: PUBLIC}, cfg, shown=55, total=57, done=True)
    assert partial.splitlines()[-1] == "✅ done · 55/57 shown"


def test_render_footer_unknown_channel_uses_id() -> None:
    cfg = FolderConfig(name="Memes")
    text = render_footer([item(1)], {}, cfg, 1, 1, False)
    assert text.splitlines()[0].endswith(f"· {PUBLIC.id}")


@pytest.fixture
def user() -> FakeUser:
    return FakeUser()


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


async def test_deliver_forwards_via_relay(user: FakeUser, bot: FakeBot) -> None:
    deliverer = Deliverer(user, bot, max_copy_bytes=10)
    kind, last_id = await deliverer.deliver(TARGET, item(msg_ids=(10, 11, 12)), PUBLIC)
    assert kind == Delivery.FORWARD
    assert user.forwarded == [(PUBLIC.id, [10, 11, 12])]
    assert bot.forwards == [(TARGET, [1000, 1001, 1002])]
    assert bot.deleted == [[1000, 1001, 1002]]
    assert last_id == 3  # three messages landed in TARGET, ids 1..3


async def test_deliver_restricted_error_falls_back_to_copy(user: FakeUser, bot: FakeBot) -> None:
    user.restricted.add(PUBLIC.id)
    deliverer = Deliverer(user, bot, max_copy_bytes=10)
    kind, last_id = await deliverer.deliver(TARGET, item(), PUBLIC)
    assert kind == Delivery.COPY
    assert user.copied == [(PUBLIC.id, [10], build_caption(PUBLIC, 10, "lol"))]
    assert bot.resent == [(TARGET, [1000], build_caption(PUBLIC, 10, "lol"))]
    assert bot.deleted == [[1000]]
    assert last_id == 1


async def test_deliver_noforwards_item_skips_forward_attempt(user: FakeUser, bot: FakeBot) -> None:
    deliverer = Deliverer(user, bot, max_copy_bytes=10)
    kind, _ = await deliverer.deliver(TARGET, item(noforwards=True), PUBLIC)
    assert kind == Delivery.COPY
    assert user.forwarded == []


async def test_deliver_too_large_falls_back_to_link(user: FakeUser, bot: FakeBot) -> None:
    user.media_bytes[(PUBLIC.id, 10)] = 11
    deliverer = Deliverer(user, bot, max_copy_bytes=10)
    kind, last_id = await deliverer.deliver(TARGET, item(noforwards=True), PUBLIC)
    assert kind == Delivery.LINK
    assert bot.texts(TARGET) == ["https://t.me/memes/10"]
    assert last_id == 1
    assert bot.deleted == []


async def test_deliver_copy_failure_falls_back_to_link(user: FakeUser, bot: FakeBot) -> None:
    user.copy_fails.add(PRIVATE.id)
    deliverer = Deliverer(user, bot, max_copy_bytes=10)
    kind, _ = await deliverer.deliver(TARGET, item(channel=PRIVATE, noforwards=True), PRIVATE)
    assert kind == Delivery.LINK
    assert bot.texts(TARGET) == ["https://t.me/c/5678/10"]


async def test_deliver_target_error_propagates(user: FakeUser, bot: FakeBot) -> None:
    bot.bad_targets.add(TARGET)
    deliverer = Deliverer(user, bot, max_copy_bytes=10)
    from telegrief.tg.protocols import TargetUnavailable

    with pytest.raises(TargetUnavailable):
        await deliverer.deliver(TARGET, item(), PUBLIC)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_delivery.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'telegrief.delivery'`

- [ ] **Step 3: Write delivery.py**

```python
"""Deliver one digest item into a target channel and render title/footer texts."""

from __future__ import annotations

import html
from datetime import date

from loguru import logger

from .config import FolderConfig
from .models import ChannelInfo, Delivery, DigestItem
from .scoring import format_count, format_engagement, format_score
from .tg.protocols import BotGateway, ForwardRestricted, GatewayError, UserGateway

CAPTION_LIMIT = 1024


def bare_id(channel_id: int) -> int:
    text = str(channel_id)
    return int(text[4:]) if text.startswith("-100") else abs(channel_id)


def post_link(channel: ChannelInfo, msg_id: int) -> str:
    if channel.username:
        return f"https://t.me/{channel.username}/{msg_id}"
    return f"https://t.me/c/{bare_id(channel.id)}/{msg_id}"


def build_caption(channel: ChannelInfo, msg_id: int, preview: str) -> str:
    head = f"↗️ {channel.display} · {post_link(channel, msg_id)}"
    text = f"{head}\n\n{preview}" if preview else head
    return text[:CAPTION_LIMIT]


def render_title(folder: str, day: date, total: int, n_channels: int) -> str:
    if total == 0:
        return f"📂 {folder} · {day:%Y-%m-%d} · no posts in window"
    return f"📂 {folder} · {day:%Y-%m-%d} · {total} posts from {n_channels} channels"


def _placeholder(channel_id: int) -> ChannelInfo:
    return ChannelInfo(
        id=channel_id,
        username=None,
        title=str(channel_id),
        subscribers=0,
        reactions_enabled=True,
        noforwards=False,
    )


def render_footer(
    batch: list[DigestItem],
    channels: dict[int, ChannelInfo],
    cfg: FolderConfig,
    shown: int,
    total: int,
    done: bool,
) -> str:
    lines: list[str] = []
    for item in batch:
        channel = channels.get(item.channel_id) or _placeholder(item.channel_id)
        lines.append(
            f"#{item.rank} · {format_score(item.score, cfg.scoring)} · "
            f"{format_engagement(item.engagement, channel)} / {format_count(item.views)}👁 · "
            f"{html.escape(channel.display)}"
        )
    if done and shown >= total:
        lines.append(f"✅ all {total} shown")
    elif done:
        lines.append(f"✅ done · {shown}/{total} shown")
    else:
        lines.append(f"shown {shown}/{total}")
    return "\n".join(lines)


class Deliverer:
    def __init__(self, user: UserGateway, bot: BotGateway, max_copy_bytes: int) -> None:
        self._user = user
        self._bot = bot
        self._max_copy_bytes = max_copy_bytes

    async def deliver(
        self, target: int, item: DigestItem, channel: ChannelInfo
    ) -> tuple[Delivery, int]:
        """Forward, else copy, else link. Returns (kind, last message id in target)."""
        msg_ids = list(item.msg_ids)
        if not item.noforwards:
            try:
                relay = await self._user.forward_to_relay(item.channel_id, msg_ids)
                sent = await self._bot.forward_from_relay(target, relay)
                await self._bot.delete_relay(relay)
                return Delivery.FORWARD, max(sent)
            except ForwardRestricted:
                logger.info("{}/{}: forward restricted, copying", item.channel_id, item.msg_ids[0])
        caption = build_caption(channel, item.msg_ids[0], item.preview)
        try:
            relay = await self._user.copy_to_relay(
                item.channel_id, msg_ids, caption, self._max_copy_bytes
            )
        except GatewayError as exc:
            logger.info(
                "{}/{}: copy skipped ({}), sending link", item.channel_id, item.msg_ids[0], exc
            )
        else:
            sent = await self._bot.resend_from_relay(target, relay, caption)
            await self._bot.delete_relay(relay)
            return Delivery.COPY, max(sent)
        link_id = await self._bot.send_text(
            target, post_link(channel, item.msg_ids[0]), preview=True
        )
        return Delivery.LINK, link_id
```

- [ ] **Step 4: Run tests and checks**

Run: `./check.sh`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/telegrief/delivery.py tests/test_delivery.py
git commit -m "feat: item delivery chain and message rendering

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Digest orchestrator

**Files:**
- Create: `src/telegrief/digest.py`
- Test: `tests/test_digest.py`

**Interfaces:**
- Consumes: everything from Tasks 2–8.
- Produces: `Deferrer = Callable[[float, Callable[[], Awaitable[None]]], None]`; `DigestService(*, user, bot, storage, collector, targets, deliverer, settings, folders, clock, defer)` with `run_all(from_h=None, to_h=None) -> list[str]`, `run_folder(cfg, from_h=None, to_h=None) -> str`, `build_digest(cfg, now, start, end) -> tuple[Digest, list[str]]`, `send_batch(digest_id) -> int`, `on_read(channel_id, max_id) -> None`, `on_next_button(digest_id) -> None`, `catch_up() -> None`, `bind() -> None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_digest.py`:

```python
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegrief.collector import Collector
from telegrief.config import FolderConfig, FoldersConfig, Settings
from telegrief.delivery import Deliverer
from telegrief.digest import DigestService
from telegrief.models import ChannelInfo, DigestStatus, ItemState, RawPost
from telegrief.storage import Storage
from telegrief.targets import TargetResolver
from tests.fakes import FakeBot, FakeUser

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
CH_ID = -1001
CH = ChannelInfo(
    id=CH_ID, username="a", title="A", subscribers=100, reactions_enabled=True, noforwards=False
)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class Deferred:
    def __init__(self) -> None:
        self.calls: list[tuple[float, Callable[[], Awaitable[None]]]] = []

    def __call__(self, delay: float, callback: Callable[[], Awaitable[None]]) -> None:
        self.calls.append((delay, callback))

    async def fire_all(self) -> None:
        calls, self.calls = self.calls, []
        for _, callback in calls:
            await callback()


class Harness:
    def __init__(self, storage: Storage, tmp_path: Path, batch_size: int = 2) -> None:
        self.user = FakeUser()
        self.bot = FakeBot()
        self.storage = storage
        self.clock = Clock()
        self.defer = Deferred()
        self.folders = FoldersConfig(folders=[FolderConfig(name="Memes", batch_size=batch_size)])
        self.settings = Settings(
            _secrets_dir=None,
            tg_api_id=1,
            tg_api_hash="h",
            bot_token="t",
            data_dir=tmp_path,
            auto_next_min_interval_s=120,
        )
        self.collector = Collector(self.user, storage)
        self.targets = TargetResolver(self.user, storage, self.bot.username)
        self.deliverer = Deliverer(self.user, self.bot, self.settings.max_copy_bytes)
        self.service = DigestService(
            user=self.user,
            bot=self.bot,
            storage=storage,
            collector=self.collector,
            targets=self.targets,
            deliverer=self.deliverer,
            settings=self.settings,
            folders=lambda: self.folders,
            clock=self.clock,
            defer=self.defer,
        )
        self.service.bind()
        self.user.folders["Memes"] = [CH_ID]
        self.user.channels[CH_ID] = CH

    def add_posts(self, n: int) -> None:
        self.user.posts[CH_ID] = [
            RawPost(
                channel_id=CH_ID,
                msg_id=i,
                grouped_id=None,
                date=NOW - timedelta(hours=30),
                views=1000,
                forwards=0,
                reactions=100 - i,
                replies=0,
                preview=f"p{i}",
                noforwards=False,
            )
            for i in range(1, n + 1)
        ]

    @property
    def target(self) -> int:
        return next(iter(self.bot.messages))


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = await Storage.open(tmp_path / "t.db")
    yield s
    await s.close()


@pytest.fixture
def h(storage: Storage, tmp_path: Path) -> Harness:
    return Harness(storage, tmp_path)


async def test_run_folder_creates_digest_and_sends_first_batch(h: Harness) -> None:
    h.add_posts(5)
    result = await h.service.run_folder(h.folders.folders[0])
    assert result == "✅ Memes: 5 posts"
    assert h.user.created == [("Memes · digest", "telegrief_bot")]
    target = h.target
    texts = h.bot.texts(target)
    assert texts[0] == "📂 Memes · 2026-10-14 · 5 posts from 1 channels"
    assert h.user.forwarded == [(CH_ID, [1]), (CH_ID, [2])]
    footer_id, footer, next_id = h.bot.last(target)
    assert footer.splitlines()[-1] == "shown 2/5"
    assert next_id == 1
    digest = await h.storage.get_digest(1)
    assert digest is not None
    assert digest.status == DigestStatus.OPEN
    assert digest.footer_msg_id == footer_id
    assert digest.footer_text == footer
    assert digest.last_batch_at == NOW
    assert await h.storage.max_sent_msg_id(1) == 3  # title=1, posts=2,3, footer=4


async def test_window_uses_settings_and_overrides(h: Harness) -> None:
    h.add_posts(1)
    await h.service.run_folder(h.folders.folders[0])
    digest = await h.storage.get_digest(1)
    assert digest is not None
    assert digest.window_start == NOW - timedelta(hours=48)
    assert digest.window_end == NOW - timedelta(hours=24)
    h.user.posts[CH_ID][0] = replace(h.user.posts[CH_ID][0], date=NOW - timedelta(hours=1))
    result = await h.service.run_folder(h.folders.folders[0], from_h=24, to_h=0)
    assert result == "✅ Memes: 1 posts"
    digest = await h.storage.get_digest(2)
    assert digest is not None and digest.window_end == NOW


async def test_empty_digest_is_done_without_footer(h: Harness) -> None:
    await h.service.run_folder(h.folders.folders[0])
    target = h.target
    assert h.bot.texts(target) == ["📂 Memes · 2026-10-14 · no posts in window"]
    digest = await h.storage.get_digest(1)
    assert digest is not None and digest.status == DigestStatus.DONE


async def test_on_read_below_marker_does_nothing(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
    h.clock.advance(600)
    await h.user.emit_read(h.target, 2)
    assert len(h.user.forwarded) == 2
    assert h.defer.calls == []


async def test_on_read_at_marker_sends_next_batch(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
    target = h.target
    h.clock.advance(120)
    await h.user.emit_read(target, 3)
    assert h.user.forwarded == [(CH_ID, [1]), (CH_ID, [2]), (CH_ID, [3]), (CH_ID, [4])]
    assert h.bot.last(target)[1].splitlines()[-1] == "shown 4/5"
    # previous footer lost its button
    assert h.bot.edits == [(target, 4, h.bot.messages[target][3][1], None)]


async def test_on_read_within_interval_defers_instead_of_dropping(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
    target = h.target
    h.clock.advance(30)
    await h.user.emit_read(target, 3)
    assert len(h.user.forwarded) == 2
    assert [d for d, _ in h.defer.calls] == [90.0]
    await h.user.emit_read(target, 3)  # second event while recheck pending: no new timer
    assert len(h.defer.calls) == 1
    h.clock.advance(90)
    await h.defer.fire_all()
    assert len(h.user.forwarded) == 4


async def test_on_read_ignores_unknown_channel(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
    h.clock.advance(600)
    await h.user.emit_read(-100999, 10_000)
    assert len(h.user.forwarded) == 2


async def test_stale_digest_ignored_after_new_run(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
    target = h.target
    h.clock.advance(24 * 3600)
    await h.service.run_folder(h.folders.folders[0])
    first = await h.storage.get_digest(1)
    assert first is not None and first.status == DigestStatus.DONE
    before = len(h.user.forwarded)
    h.clock.advance(600)
    await h.user.emit_read(target, 3)  # marker only past digest 1's batch
    assert len(h.user.forwarded) == before


async def test_next_button_bypasses_pacing(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
    await h.bot.press_next(1)
    assert len(h.user.forwarded) == 4


async def test_last_batch_marks_done(h: Harness) -> None:
    h.add_posts(3)
    await h.service.run_folder(h.folders.folders[0])
    target = h.target
    await h.bot.press_next(1)
    digest = await h.storage.get_digest(1)
    assert digest is not None and digest.status == DigestStatus.DONE
    _, footer, next_id = h.bot.last(target)
    assert footer.splitlines()[-1] == "✅ all 3 shown"
    assert next_id is None
    await h.bot.press_next(1)  # done digest: nothing happens
    assert len(h.user.forwarded) == 3


async def test_item_failure_does_not_stop_batch(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h.add_posts(3)
    original = h.deliverer.deliver

    async def flaky(target: int, item: object, channel: object) -> object:
        if getattr(item, "rank") == 1:
            raise RuntimeError("boom")
        return await original(target, item, channel)  # type: ignore[arg-type]

    monkeypatch.setattr(h.deliverer, "deliver", flaky)
    await h.service.run_folder(h.folders.folders[0])
    assert await h.storage.count_items(1, ItemState.FAILED) == 1
    assert await h.storage.count_items(1, ItemState.SENT) == 1
    assert h.bot.last(h.target)[1].splitlines()[-1] == "shown 1/3"


async def test_run_folder_reports_failure_to_owner(h: Harness) -> None:
    del h.user.folders["Memes"]
    result = await h.service.run_folder(h.folders.folders[0])
    assert result.startswith("❌ Memes:")
    assert h.bot.owner_messages == [result]


async def test_run_folder_marks_failed_when_target_unusable(h: Harness) -> None:
    h.add_posts(2)
    h.bot.bad_targets.add(-1009000000001)
    result = await h.service.run_folder(h.folders.folders[0])
    assert result.startswith("❌ Memes:")
    digest = await h.storage.get_digest(1)
    assert digest is not None and digest.status == DigestStatus.FAILED


async def test_run_folder_mentions_skipped_channels(h: Harness) -> None:
    h.add_posts(1)
    h.user.folders["Memes"].append(-1002)
    result = await h.service.run_folder(h.folders.folders[0])
    assert result == "✅ Memes: 1 posts · ⚠️ skipped: -1002: -1002"


async def test_run_all_runs_every_folder(h: Harness) -> None:
    h.add_posts(1)
    assert await h.service.run_all() == ["✅ Memes: 1 posts"]


async def test_catch_up_sends_next_when_marker_already_past(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
    h.user.read_markers[h.target] = 3
    h.clock.advance(600)
    await h.service.catch_up()
    assert len(h.user.forwarded) == 4


async def test_send_batch_without_folder_config_is_noop(h: Harness) -> None:
    h.add_posts(5)
    await h.service.run_folder(h.folders.folders[0])
    h.folders = FoldersConfig(folders=[FolderConfig(name="Other")])
    assert await h.service.send_batch(1) == 0
    assert len(h.user.forwarded) == 2


async def test_ping_and_bind(h: Harness) -> None:
    assert h.user._read_handler is not None
    assert h.bot._next_handler is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_digest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'telegrief.digest'`

- [ ] **Step 3: Write digest.py**

```python
"""Digest lifecycle: build, send batches, auto-next on read, manual next."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from loguru import logger

from .collector import Collector, window
from .config import FolderConfig, FoldersConfig, Settings
from .delivery import Deliverer, render_footer, render_title
from .models import ChannelInfo, Digest, DigestStatus, ItemState
from .scoring import rank
from .targets import TargetResolver
from .tg.protocols import BotGateway, ChannelUnavailable, GatewayError, UserGateway

if TYPE_CHECKING:
    from .storage import Storage

Deferrer = Callable[[float, Callable[[], Awaitable[None]]], None]


def _asyncio_defer(delay: float, callback: Callable[[], Awaitable[None]]) -> None:
    loop = asyncio.get_running_loop()
    loop.call_later(delay, lambda: loop.create_task(callback()))


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DigestService:
    def __init__(
        self,
        *,
        user: UserGateway,
        bot: BotGateway,
        storage: Storage,
        collector: Collector,
        targets: TargetResolver,
        deliverer: Deliverer,
        settings: Settings,
        folders: Callable[[], FoldersConfig],
        clock: Callable[[], datetime] = _utcnow,
        defer: Deferrer = _asyncio_defer,
    ) -> None:
        self._user = user
        self._bot = bot
        self._storage = storage
        self._collector = collector
        self._targets = targets
        self._deliverer = deliverer
        self._settings = settings
        self._folders = folders
        self._clock = clock
        self._defer = defer
        self._locks: dict[int, asyncio.Lock] = {}
        self._rechecks: set[int] = set()

    def bind(self) -> None:
        self._user.on_channel_read(self.on_read)
        self._bot.on_next_button(self.on_next_button)

    # ---- runs ----------------------------------------------------------------

    async def run_all(self, from_h: int | None = None, to_h: int | None = None) -> list[str]:
        return [await self.run_folder(cfg, from_h, to_h) for cfg in self._folders().folders]

    async def run_folder(
        self, cfg: FolderConfig, from_h: int | None = None, to_h: int | None = None
    ) -> str:
        now = self._clock()
        start, end = window(
            now,
            from_h if from_h is not None else self._settings.window_from_h,
            to_h if to_h is not None else self._settings.window_to_h,
        )
        try:
            digest, errors = await self.build_digest(cfg, now, start, end)
        except Exception as exc:
            logger.exception("folder {}: run failed", cfg.name)
            message = f"❌ {cfg.name}: {exc}"
            await self._bot.send_owner(message)
            return message
        message = f"✅ {cfg.name}: {digest.total} posts"
        if errors:
            message += f" · ⚠️ skipped: {', '.join(errors)}"
        logger.info(message)
        return message

    async def build_digest(
        self, cfg: FolderConfig, now: datetime, start: datetime, end: datetime
    ) -> tuple[Digest, list[str]]:
        target = await self._targets.resolve(cfg, now)
        collected = await self._collector.collect(cfg.name, start, end, now)
        scored = rank(collected.posts, collected.channels, cfg)
        digest = await self._storage.create_digest(cfg.name, target, start, end, scored, now)
        day = end.astimezone(ZoneInfo(self._settings.tz)).date()
        title = render_title(cfg.name, day, digest.total, len({s.channel.id for s in scored}))
        try:
            digest.title_msg_id = await self._bot.send_text(target, title)
        except GatewayError:
            digest.status = DigestStatus.FAILED
            await self._storage.update_digest(digest)
            raise
        if digest.total == 0:
            digest.status = DigestStatus.DONE
            await self._storage.update_digest(digest)
            return digest, collected.errors
        await self._storage.update_digest(digest)
        try:
            await self.send_batch(digest.id)
        except GatewayError:
            digest.status = DigestStatus.FAILED
            await self._storage.update_digest(digest)
            raise
        return digest, collected.errors

    # ---- batches -------------------------------------------------------------

    async def _channel(self, channel_id: int, now: datetime) -> ChannelInfo:
        try:
            return await self._collector.channel_info(channel_id, now)
        except ChannelUnavailable:
            return ChannelInfo(
                id=channel_id,
                username=None,
                title=str(channel_id),
                subscribers=0,
                reactions_enabled=True,
                noforwards=False,
            )

    async def send_batch(self, digest_id: int) -> int:
        """Deliver the next batch of pending items. Returns how many were attempted."""
        lock = self._locks.setdefault(digest_id, asyncio.Lock())
        async with lock:
            digest = await self._storage.get_digest(digest_id)
            if digest is None or digest.status != DigestStatus.OPEN:
                return 0
            cfg = self._folders().get(digest.folder)
            if cfg is None:
                logger.warning("digest {}: folder {} no longer configured", digest_id, digest.folder)
                return 0
            now = self._clock()
            items = await self._storage.pending_items(digest.id, cfg.batch_size)
            channels: dict[int, ChannelInfo] = {}
            for item in items:
                channel = channels.get(item.channel_id)
                if channel is None:
                    channel = channels[item.channel_id] = await self._channel(item.channel_id, now)
                try:
                    kind, last_id = await self._deliverer.deliver(digest.target, item, channel)
                except GatewayError:
                    raise
                except Exception:
                    logger.exception("digest {}: item #{} failed", digest.id, item.rank)
                    item.state = ItemState.FAILED
                else:
                    item.state = ItemState.SENT
                    item.delivery = kind
                    item.sent_msg_id = last_id
                    item.sent_at = now
                await self._storage.update_item(item)
            shown = await self._storage.count_items(digest.id, ItemState.SENT)
            pending = await self._storage.count_items(digest.id, ItemState.PENDING)
            done = pending == 0
            text = render_footer(
                [i for i in items if i.state == ItemState.SENT],
                channels,
                cfg,
                shown,
                digest.total,
                done,
            )
            if digest.footer_msg_id is not None and digest.footer_text is not None:
                try:
                    await self._bot.edit_text(digest.target, digest.footer_msg_id, digest.footer_text)
                except GatewayError as exc:
                    logger.warning("digest {}: could not edit old footer: {}", digest.id, exc)
            digest.footer_msg_id = await self._bot.send_text(
                digest.target,
                text,
                next_digest_id=None if done else digest.id,
                next_label=f"Next {cfg.batch_size}",
            )
            digest.footer_text = text
            digest.last_batch_at = now
            if done:
                digest.status = DigestStatus.DONE
            await self._storage.update_digest(digest)
            return len(items)

    async def _guarded_send(self, digest_id: int) -> None:
        try:
            await self.send_batch(digest_id)
        except Exception as exc:
            logger.exception("digest {}: batch failed", digest_id)
            await self._bot.send_owner(f"❌ digest {digest_id}: {exc}")

    # ---- triggers ------------------------------------------------------------

    async def on_read(self, channel_id: int, max_id: int) -> None:
        digest = await self._storage.open_digest_for_target(channel_id)
        if digest is None:
            return
        last = await self._storage.max_sent_msg_id(digest.id)
        if last is None or max_id < last:
            return
        if await self._storage.count_items(digest.id, ItemState.PENDING) == 0:
            return
        interval = float(self._settings.auto_next_min_interval_s)
        elapsed = (
            (self._clock() - digest.last_batch_at).total_seconds()
            if digest.last_batch_at is not None
            else interval
        )
        if elapsed >= interval:
            await self._guarded_send(digest.id)
            return
        if digest.id in self._rechecks:
            return
        self._rechecks.add(digest.id)
        target = digest.target
        digest_id = digest.id

        async def recheck() -> None:
            self._rechecks.discard(digest_id)
            marker = await self._user.read_marker(target)
            await self.on_read(target, marker)

        self._defer(interval - elapsed, recheck)

    async def on_next_button(self, digest_id: int) -> None:
        await self._guarded_send(digest_id)

    async def catch_up(self) -> None:
        for digest in await self._storage.open_digests():
            marker = await self._user.read_marker(digest.target)
            await self.on_read(digest.target, marker)
```

- [ ] **Step 4: Run tests and checks**

Run: `./check.sh`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/telegrief/digest.py tests/test_digest.py
git commit -m "feat: digest orchestrator with read-paced batches

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Owner commands

**Files:**
- Create: `src/telegrief/commands.py`
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: `DigestService` (Task 9), `Storage` (Task 4), `Collector` (Task 6), `TargetResolver` (Task 7), `UserGateway` (Task 5), `FoldersConfig` (Task 2), `format_count` (Task 3).
- Produces: `parse_run_args(args) -> tuple[str | None, int | None, int | None]`, `CommandHandler(service, user, storage, collector, targets, folders, clock)` with `handle(command, args) -> str` (matches `CommandHandlerFn`), `USAGE`.

- [ ] **Step 1: Write the failing tests**

`tests/test_commands.py`:

```python
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegrief.collector import Collector
from telegrief.commands import USAGE, CommandHandler, parse_run_args
from telegrief.config import FolderConfig, FoldersConfig, Settings
from telegrief.delivery import Deliverer
from telegrief.digest import DigestService
from telegrief.models import ChannelInfo, RawPost
from telegrief.storage import Storage
from telegrief.targets import TargetResolver
from tests.fakes import FakeBot, FakeUser

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
CH_ID = -1001
CH = ChannelInfo(
    id=CH_ID, username="a", title="Alpha", subscribers=2900, reactions_enabled=True,
    noforwards=False,
)


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = await Storage.open(tmp_path / "t.db")
    yield s
    await s.close()


class Harness:
    def __init__(self, storage: Storage, tmp_path: Path) -> None:
        self.user = FakeUser()
        self.bot = FakeBot()
        self.storage = storage
        self.folders = FoldersConfig(
            folders=[FolderConfig(name="Memes", batch_size=2), FolderConfig(name="My News")]
        )
        settings = Settings(
            _secrets_dir=None, tg_api_id=1, tg_api_hash="h", bot_token="t", data_dir=tmp_path
        )
        collector = Collector(self.user, storage)
        targets = TargetResolver(self.user, storage, self.bot.username)
        self.service = DigestService(
            user=self.user,
            bot=self.bot,
            storage=storage,
            collector=collector,
            targets=targets,
            deliverer=Deliverer(self.user, self.bot, settings.max_copy_bytes),
            settings=settings,
            folders=lambda: self.folders,
            clock=lambda: NOW,
        )
        self.handler = CommandHandler(
            self.service, self.user, storage, collector, targets, lambda: self.folders, lambda: NOW
        )
        self.bot.on_owner_command(self.handler.handle)
        self.user.folders["Memes"] = [CH_ID]
        self.user.folders["My News"] = []
        self.user.channels[CH_ID] = CH
        self.user.posts[CH_ID] = [
            RawPost(
                channel_id=CH_ID,
                msg_id=i,
                grouped_id=None,
                date=NOW - timedelta(hours=30),
                views=100,
                forwards=0,
                reactions=10 - i,
                replies=0,
                preview="",
                noforwards=False,
            )
            for i in range(1, 4)
        ]


@pytest.fixture
def h(storage: Storage, tmp_path: Path) -> Harness:
    return Harness(storage, tmp_path)


def test_parse_run_args() -> None:
    assert parse_run_args([]) == (None, None, None)
    assert parse_run_args(["Memes"]) == ("Memes", None, None)
    assert parse_run_args(["Memes", "24"]) == ("Memes", 24, None)
    assert parse_run_args(["Memes", "24", "0"]) == ("Memes", 24, 0)
    assert parse_run_args(["24", "0"]) == (None, 24, 0)


def test_run_parses_folder_with_spaces() -> None:
    assert parse_run_args(["My", "News", "24", "0"]) == ("My News", 24, 0)


async def test_run_one_folder(h: Harness) -> None:
    assert await h.bot.command("run", ["Memes"]) == "✅ Memes: 3 posts"


async def test_run_all_folders(h: Harness) -> None:
    assert await h.bot.command("run", []) == "✅ Memes: 3 posts\n✅ My News: 0 posts"


async def test_run_unknown_folder(h: Harness) -> None:
    assert await h.bot.command("run", ["Nope"]) == "unknown folder: Nope"


async def test_run_rejects_inverted_hours(h: Harness) -> None:
    assert await h.bot.command("run", ["Memes", "0", "24"]) == "from_h must be greater than to_h"


async def test_status_none_and_open(h: Harness) -> None:
    assert await h.bot.command("status", []) == "Memes: none\nMy News: none"
    await h.bot.command("run", ["Memes"])
    assert (
        await h.bot.command("status", [])
        == "Memes: 2026-10-14 · open · shown 2/3 · pending 1\nMy News: none"
    )


async def test_folders_lists_resolved_channels(h: Harness) -> None:
    text = await h.bot.command("folders", [])
    assert text == "Memes (1):\n  Alpha (@a) · 2.9k\nMy News (0):"


async def test_folders_reports_missing_folder(h: Harness) -> None:
    del h.user.folders["My News"]
    text = await h.bot.command("folders", [])
    assert text.splitlines()[-1] == "My News: ❌ folder not found"


async def test_reset(h: Harness) -> None:
    assert await h.bot.command("reset", []) == USAGE
    assert await h.bot.command("reset", ["Memes"]) == "no digest channel mapping for Memes"
    await h.bot.command("run", ["Memes"])
    assert await h.bot.command("reset", ["Memes"]) == "reset Memes; next run creates a new channel"


async def test_unknown_command_shows_usage(h: Harness) -> None:
    assert await h.bot.command("help", []) == USAGE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_commands.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'telegrief.commands'`

- [ ] **Step 3: Write commands.py**

```python
"""Owner-only bot commands: /run, /status, /folders, /reset."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from .collector import Collector
from .config import FoldersConfig
from .digest import DigestService
from .models import ItemState
from .scoring import format_count
from .targets import TargetResolver
from .tg.protocols import FolderNotFound, UserGateway

if TYPE_CHECKING:
    from .storage import Storage

USAGE = (
    "/run [folder] [from_h] [to_h] — run now (defaults: 48 24)\n"
    "/status — open digests\n"
    "/folders — channels resolved per folder\n"
    "/reset <folder> — forget digest channel mapping"
)


def parse_run_args(args: list[str]) -> tuple[str | None, int | None, int | None]:
    """Trailing integers (up to two) are hours; the rest, joined, is the folder."""
    words = list(args)
    hours: list[int] = []
    while words and len(hours) < 2 and words[-1].lstrip("-").isdigit():
        hours.insert(0, int(words.pop()))
    folder = " ".join(words) or None
    from_h = hours[0] if hours else None
    to_h = hours[1] if len(hours) > 1 else None
    return folder, from_h, to_h


class CommandHandler:
    def __init__(
        self,
        service: DigestService,
        user: UserGateway,
        storage: Storage,
        collector: Collector,
        targets: TargetResolver,
        folders: Callable[[], FoldersConfig],
        clock: Callable[[], datetime],
    ) -> None:
        self._service = service
        self._user = user
        self._storage = storage
        self._collector = collector
        self._targets = targets
        self._folders = folders
        self._clock = clock

    async def handle(self, command: str, args: list[str]) -> str:
        if command == "run":
            return await self._run(args)
        if command == "status":
            return await self._status()
        if command == "folders":
            return await self._list_folders()
        if command == "reset":
            return await self._reset(args)
        return USAGE

    async def _run(self, args: list[str]) -> str:
        folder, from_h, to_h = parse_run_args(args)
        if from_h is not None and to_h is not None and from_h <= to_h:
            return "from_h must be greater than to_h"
        if folder is None:
            return "\n".join(await self._service.run_all(from_h, to_h))
        cfg = self._folders().get(folder)
        if cfg is None:
            return f"unknown folder: {folder}"
        return await self._service.run_folder(cfg, from_h, to_h)

    async def _status(self) -> str:
        lines: list[str] = []
        for cfg in self._folders().folders:
            digest = await self._storage.latest_digest(cfg.name)
            if digest is None:
                lines.append(f"{cfg.name}: none")
                continue
            sent = await self._storage.count_items(digest.id, ItemState.SENT)
            pending = await self._storage.count_items(digest.id, ItemState.PENDING)
            lines.append(
                f"{cfg.name}: {digest.window_end:%Y-%m-%d} · {digest.status} · "
                f"shown {sent}/{digest.total} · pending {pending}"
            )
        return "\n".join(lines)

    async def _list_folders(self) -> str:
        now = self._clock()
        lines: list[str] = []
        for cfg in self._folders().folders:
            try:
                channel_ids = await self._user.folder_channels(cfg.name)
            except FolderNotFound:
                lines.append(f"{cfg.name}: ❌ folder not found")
                continue
            lines.append(f"{cfg.name} ({len(channel_ids)}):")
            for channel_id in channel_ids:
                try:
                    info = await self._collector.channel_info(channel_id, now)
                except Exception as exc:
                    lines.append(f"  {channel_id} · ❌ {exc}")
                    continue
                lines.append(
                    f"  {info.title} ({info.display}) · {format_count(info.subscribers)}"
                )
        return "\n".join(lines)

    async def _reset(self, args: list[str]) -> str:
        folder = " ".join(args)
        if not folder:
            return USAGE
        if await self._targets.reset(folder):
            return f"reset {folder}; next run creates a new channel"
        return f"no digest channel mapping for {folder}"
```

- [ ] **Step 4: Run tests and checks**

Run: `./check.sh`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/telegrief/commands.py tests/test_commands.py
git commit -m "feat: owner bot commands

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Telethon user gateway

**Files:**
- Create: `src/telegrief/tg/user.py`
- Test: `tests/test_tg_user.py` (pure helpers only; no live Telegram)

**Interfaces:**
- Consumes: `UserGateway` + exceptions (Task 5), `ChannelInfo`, `RawPost` (Task 2).
- Produces: `filter_title(dialog_filter) -> str`, `select_channel_ids(*, include, pinned, exclude, broadcasts, all_broadcasts) -> list[int]`, `is_broadcast(entity) -> bool`, `raw_post_from_message(msg, channel_id) -> RawPost | None`, `TelethonUser` (implements `UserGateway`) with `connect(session, api_id, api_hash, tmp_dir) -> TelethonUser`, `bot_username` attribute set by `serve`, `run_until_disconnected()`, `disconnect()`; `login(session, api_id, api_hash) -> None` interactive helper.

- [ ] **Step 1: Write the failing helper tests**

`tests/test_tg_user.py`:

```python
from datetime import UTC, datetime
from types import SimpleNamespace

from telegrief.tg.user import filter_title, raw_post_from_message, select_channel_ids

T = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def test_filter_title_handles_plain_str() -> None:
    assert filter_title(SimpleNamespace(title="Memes")) == "Memes"


def test_filter_title_handles_text_with_entities() -> None:
    assert filter_title(SimpleNamespace(title=SimpleNamespace(text="Memes 🐸"))) == "Memes 🐸"


def test_select_channel_ids_union_minus_exclude_keeps_order() -> None:
    ids = select_channel_ids(
        include=[-1003, -1001], pinned=[-1002], exclude=[-1003], broadcasts=False,
        all_broadcasts=[-1009],
    )
    assert ids == [-1002, -1001]


def test_select_channel_ids_broadcasts_flag_adds_all() -> None:
    ids = select_channel_ids(
        include=[-1001], pinned=[], exclude=[-1009], broadcasts=True,
        all_broadcasts=[-1001, -1008, -1009],
    )
    assert ids == [-1001, -1008]


def message(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "id": 10,
        "date": T,
        "grouped_id": None,
        "views": 500,
        "forwards": 3,
        "reactions": SimpleNamespace(results=[SimpleNamespace(count=4), SimpleNamespace(count=6)]),
        "replies": SimpleNamespace(replies=2),
        "message": "line one\nline two " + "x" * 100,
        "noforwards": False,
        "action": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_raw_post_from_message_maps_fields() -> None:
    post = raw_post_from_message(message(), -1001)
    assert post is not None
    assert post.channel_id == -1001 and post.msg_id == 10 and post.date == T
    assert post.views == 500 and post.forwards == 3 and post.reactions == 10 and post.replies == 2
    assert post.preview.startswith("line one line two ") and len(post.preview) == 80
    assert post.noforwards is False


def test_raw_post_from_message_handles_missing_counters() -> None:
    post = raw_post_from_message(
        message(views=None, forwards=None, reactions=None, replies=None, message=None), -1001
    )
    assert post is not None
    assert (post.views, post.forwards, post.reactions, post.replies, post.preview) == (0, 0, 0, 0, "")


def test_raw_post_from_message_empty_reactions_results() -> None:
    post = raw_post_from_message(message(reactions=SimpleNamespace(results=[])), -1001)
    assert post is not None and post.reactions == 0


def test_raw_post_from_message_skips_service_messages() -> None:
    assert raw_post_from_message(message(action=SimpleNamespace()), -1001) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tg_user.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'telegrief.tg.user'`

- [ ] **Step 3: Write tg/user.py**

```python
"""Telethon user-session gateway: reading, channel creation, relay uploads."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from telethon import TelegramClient, events, utils
from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    ChatForwardsRestrictedError,
    RPCError,
)
from telethon.tl import functions, types

from ..models import ChannelInfo, RawPost
from .protocols import (
    ChannelUnavailable,
    CopyFailed,
    FolderNotFound,
    ForwardRestricted,
    MediaTooLarge,
    ReadHandler,
)

PREVIEW_CHARS = 80
FLOOD_SLEEP_THRESHOLD = 120


def filter_title(dialog_filter: Any) -> str:
    title = getattr(dialog_filter, "title", "")
    return str(getattr(title, "text", title))


def select_channel_ids(
    *,
    include: Iterable[int],
    pinned: Iterable[int],
    exclude: Iterable[int],
    broadcasts: bool,
    all_broadcasts: Iterable[int],
) -> list[int]:
    chosen: dict[int, None] = {}
    for channel_id in (*pinned, *include):
        chosen.setdefault(channel_id, None)
    if broadcasts:
        for channel_id in all_broadcasts:
            chosen.setdefault(channel_id, None)
    for channel_id in exclude:
        chosen.pop(channel_id, None)
    return list(chosen)


def is_broadcast(entity: Any) -> bool:
    return isinstance(entity, types.Channel) and bool(entity.broadcast) and not entity.megagroup


def raw_post_from_message(msg: Any, channel_id: int) -> RawPost | None:
    if getattr(msg, "action", None) is not None or msg.date is None:
        return None
    reactions = 0
    if getattr(msg, "reactions", None) is not None:
        reactions = sum(int(r.count) for r in msg.reactions.results)
    replies_obj = getattr(msg, "replies", None)
    replies = int(replies_obj.replies) if replies_obj is not None else 0
    text = " ".join((msg.message or "").split())
    return RawPost(
        channel_id=channel_id,
        msg_id=int(msg.id),
        grouped_id=msg.grouped_id,
        date=msg.date,
        views=int(msg.views or 0),
        forwards=int(msg.forwards or 0),
        reactions=reactions,
        replies=replies,
        preview=text[:PREVIEW_CHARS],
        noforwards=bool(getattr(msg, "noforwards", False)),
    )


def _ids(messages: Any) -> list[int]:
    items = messages if isinstance(messages, list) else [messages]
    return [int(m.id) for m in items if m is not None]


class TelethonUser:
    def __init__(self, client: TelegramClient, me_id: int, tmp_dir: Path) -> None:
        self._client = client
        self.me_id = me_id
        self._tmp = tmp_dir
        self.bot_username = ""

    @classmethod
    async def connect(
        cls, session: Path, api_id: int, api_hash: str, tmp_dir: Path
    ) -> TelethonUser:
        client = TelegramClient(
            str(session), api_id, api_hash, flood_sleep_threshold=FLOOD_SLEEP_THRESHOLD
        )
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("user session is not authorized; run `telegrief login` first")
        me = await client.get_me()
        logger.info("user session: {} (id {})", me.first_name, me.id)
        return cls(client, int(me.id), tmp_dir)

    async def run_until_disconnected(self) -> None:
        await self._client.run_until_disconnected()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    # ---- folders -------------------------------------------------------------

    async def folder_channels(self, folder_name: str) -> list[int]:
        result = await self._client(functions.messages.GetDialogFiltersRequest())
        filters = getattr(result, "filters", result)
        for dialog_filter in filters:
            if not isinstance(dialog_filter, types.DialogFilter | types.DialogFilterChatlist):
                continue
            if filter_title(dialog_filter) != folder_name:
                continue
            include = list(dialog_filter.include_peers)
            pinned = list(dialog_filter.pinned_peers)
            exclude = list(getattr(dialog_filter, "exclude_peers", []))
            peers = {utils.get_peer_id(p): p for p in (*pinned, *include)}
            all_broadcasts: list[int] = []
            if getattr(dialog_filter, "broadcasts", False):
                async for dialog in self._client.iter_dialogs():
                    if is_broadcast(dialog.entity):
                        all_broadcasts.append(int(dialog.id))
            ids = select_channel_ids(
                include=[utils.get_peer_id(p) for p in include],
                pinned=[utils.get_peer_id(p) for p in pinned],
                exclude=[utils.get_peer_id(p) for p in exclude],
                broadcasts=bool(getattr(dialog_filter, "broadcasts", False)),
                all_broadcasts=all_broadcasts,
            )
            channels: list[int] = []
            for channel_id in ids:
                if await self._is_broadcast_channel(peers.get(channel_id, channel_id)):
                    channels.append(channel_id)
            return channels
        raise FolderNotFound(folder_name)

    async def _is_broadcast_channel(self, peer: Any) -> bool:
        try:
            entity = await self._client.get_entity(peer)
        except (ValueError, RPCError) as exc:
            logger.warning("cannot resolve {}: {}", peer, exc)
            return False
        return is_broadcast(entity)

    # ---- channels and posts --------------------------------------------------

    async def channel_info(self, channel_id: int) -> ChannelInfo:
        try:
            entity = await self._client.get_entity(channel_id)
            full = await self._client(functions.channels.GetFullChannelRequest(entity))
        except (ChannelPrivateError, ChannelInvalidError, ValueError) as exc:
            raise ChannelUnavailable(f"{channel_id}: {exc}") from exc
        full_chat = full.full_chat
        return ChannelInfo(
            id=utils.get_peer_id(entity),
            username=entity.username,
            title=entity.title,
            subscribers=int(full_chat.participants_count or 0),
            reactions_enabled=not isinstance(
                full_chat.available_reactions, types.ChatReactionsNone
            ),
            noforwards=bool(entity.noforwards),
        )

    async def iter_posts(
        self, channel_id: int, start: datetime, end: datetime
    ) -> AsyncIterator[RawPost]:
        try:
            async for msg in self._client.iter_messages(channel_id, offset_date=end):
                if msg.date < start:
                    break
                post = raw_post_from_message(msg, channel_id)
                if post is not None:
                    yield post
        except (ChannelPrivateError, ChannelInvalidError, ValueError) as exc:
            raise ChannelUnavailable(f"{channel_id}: {exc}") from exc

    # ---- digest channel ------------------------------------------------------

    async def create_digest_channel(self, title: str, bot_username: str) -> int:
        result = await self._client(
            functions.channels.CreateChannelRequest(
                title=title, about="telegrief digest", broadcast=True
            )
        )
        channel = result.chats[0]
        bot = await self._client.get_input_entity(bot_username)
        try:
            await self._client(functions.channels.InviteToChannelRequest(channel, [bot]))
        except RPCError as exc:  # bots are often addable only via EditAdmin
            logger.debug("invite bot: {}", exc)
        await self._client(
            functions.channels.EditAdminRequest(
                channel,
                bot,
                types.ChatAdminRights(post_messages=True, edit_messages=True, delete_messages=True),
                rank="digest",
            )
        )
        return int(utils.get_peer_id(channel))

    # ---- relay ---------------------------------------------------------------

    async def forward_to_relay(self, channel_id: int, msg_ids: list[int]) -> list[int]:
        try:
            sent = await self._client.forward_messages(
                self.bot_username, msg_ids, from_peer=channel_id, silent=True
            )
        except ChatForwardsRestrictedError as exc:
            raise ForwardRestricted(str(exc)) from exc
        return _ids(sent)

    async def copy_to_relay(
        self, channel_id: int, msg_ids: list[int], caption: str, max_bytes: int
    ) -> list[int]:
        messages = await self._client.get_messages(channel_id, ids=msg_ids)
        messages = [m for m in messages if m is not None]
        if not messages:
            raise CopyFailed("source messages not found")
        total = sum(int(m.file.size or 0) for m in messages if m.file is not None)
        if total > max_bytes:
            raise MediaTooLarge(f"{total} bytes > {max_bytes}")
        self._tmp.mkdir(parents=True, exist_ok=True)
        files: list[str] = []
        try:
            for m in messages:
                if m.media is None or isinstance(m.media, types.MessageMediaWebPage):
                    continue
                path = await self._client.download_media(
                    m, file=str(self._tmp / f"{abs(channel_id)}_{m.id}")
                )
                if path:
                    files.append(str(path))
            if not files:
                sent = await self._client.send_message(self.bot_username, caption, silent=True)
            else:
                sent = await self._client.send_file(
                    self.bot_username, files, caption=caption, silent=True
                )
        except (RPCError, OSError) as exc:
            raise CopyFailed(str(exc)) from exc
        finally:
            for path in files:
                Path(path).unlink(missing_ok=True)
        return _ids(sent)

    # ---- read tracking -------------------------------------------------------

    async def read_marker(self, channel_id: int) -> int:
        peer = await self._client.get_input_entity(channel_id)
        result = await self._client(
            functions.messages.GetPeerDialogsRequest(peers=[types.InputDialogPeer(peer)])
        )
        return int(result.dialogs[0].read_inbox_max_id) if result.dialogs else 0

    def on_channel_read(self, handler: ReadHandler) -> None:
        async def _on_update(update: types.UpdateReadChannelInbox) -> None:
            marked = utils.get_peer_id(types.PeerChannel(update.channel_id))
            await handler(int(marked), int(update.max_id))

        self._client.add_event_handler(_on_update, events.Raw(types.UpdateReadChannelInbox))

    async def ping_bot(self, bot_username: str) -> None:
        await self._client.send_message(bot_username, "/start")


async def login(session: Path, api_id: int, api_hash: str) -> None:
    """Interactive one-time login (phone, code, 2FA) creating the session file."""
    client = TelegramClient(str(session), api_id, api_hash)
    await client.start()
    me = await client.get_me()
    print(f"logged in as {me.first_name} (id {me.id}); session saved to {session}.session")
    await client.disconnect()
```

- [ ] **Step 4: Run tests and checks**

Run: `./check.sh`
Expected: PASS. mypy runs with relaxed `disallow_untyped_calls` for this module (pyproject override from Task 1); if it still flags `events.Raw` or `types.*` attribute access, add `# type: ignore[attr-defined]` to that line only.

- [ ] **Step 5: Commit**

```bash
git add src/telegrief/tg/user.py tests/test_tg_user.py
git commit -m "feat: telethon user gateway

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: Telethon bot gateway

**Files:**
- Create: `src/telegrief/tg/bot.py`
- Test: `tests/test_tg_bot.py` (pure helpers only)

**Interfaces:**
- Consumes: `BotGateway` + exceptions (Task 5).
- Produces: `parse_callback(data) -> int | None`, `parse_command(text) -> tuple[str, list[str]] | None`, `next_button(digest_id, label)`, `TelethonBot` (implements `BotGateway`) with `connect(session, api_id, api_hash, token, owner_id) -> TelethonBot`, `run_until_disconnected()`, `disconnect()`.

- [ ] **Step 1: Write the failing helper tests**

`tests/test_tg_bot.py`:

```python
from telegrief.tg.bot import next_button, parse_callback, parse_command


def test_parse_callback() -> None:
    assert parse_callback(b"next:12") == 12
    assert parse_callback(b"next:") is None
    assert parse_callback(b"other:1") is None
    assert parse_callback(None) is None


def test_parse_command() -> None:
    assert parse_command("/run Memes 24 0") == ("run", ["Memes", "24", "0"])
    assert parse_command("/run@telegrief_bot Memes") == ("run", ["Memes"])
    assert parse_command("/status") == ("status", [])
    assert parse_command("hello") is None
    assert parse_command("") is None


def test_next_button() -> None:
    assert next_button(None, "Next 10") is None
    button = next_button(5, "Next 10")
    assert button is not None
    assert button.data == b"next:5"
    assert button.text == "Next 10"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tg_bot.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'telegrief.tg.bot'`

- [ ] **Step 3: Write tg/bot.py**

```python
"""Telethon bot gateway: delivery into digest channels, owner chat, buttons."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from loguru import logger
from telethon import Button, TelegramClient, events
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChatForwardsRestrictedError,
    ChatWriteForbiddenError,
    MessageNotModifiedError,
    RPCError,
)

from .protocols import CommandHandlerFn, ForwardRestricted, NextHandler, TargetUnavailable

FLOOD_SLEEP_THRESHOLD = 120
_CALLBACK = re.compile(rb"^next:(\d+)$")
_COMMAND = re.compile(r"^/([a-zA-Z_]+)(?:@\w+)?(?:\s+(.*))?$", re.DOTALL)
_TARGET_ERRORS = (ChannelPrivateError, ChatAdminRequiredError, ChatWriteForbiddenError, ValueError)


def parse_callback(data: bytes | None) -> int | None:
    if not data:
        return None
    match = _CALLBACK.match(data)
    return int(match.group(1)) if match else None


def parse_command(text: str) -> tuple[str, list[str]] | None:
    match = _COMMAND.match(text.strip()) if text else None
    if not match:
        return None
    return match.group(1).lower(), (match.group(2) or "").split()


def next_button(digest_id: int | None, label: str) -> Any:
    if digest_id is None:
        return None
    return Button.inline(label, data=f"next:{digest_id}".encode())


def _ids(messages: Any) -> list[int]:
    items = messages if isinstance(messages, list) else [messages]
    return [int(m.id) for m in items if m is not None]


class TelethonBot:
    def __init__(self, client: TelegramClient, username: str, owner_id: int) -> None:
        self._client = client
        self.username = username
        self._owner_id = owner_id

    @classmethod
    async def connect(
        cls, session: Path, api_id: int, api_hash: str, token: str, owner_id: int
    ) -> TelethonBot:
        client = TelegramClient(
            str(session), api_id, api_hash, flood_sleep_threshold=FLOOD_SLEEP_THRESHOLD
        )
        await client.start(bot_token=token)
        me = await client.get_me()
        logger.info("bot session: @{}", me.username)
        return cls(client, str(me.username), owner_id)

    async def run_until_disconnected(self) -> None:
        await self._client.run_until_disconnected()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    # ---- delivery --------------------------------------------------------------

    async def forward_from_relay(self, target: int, relay_ids: list[int]) -> list[int]:
        try:
            sent = await self._client.forward_messages(
                target, relay_ids, from_peer=self._owner_id, silent=True
            )
        except ChatForwardsRestrictedError as exc:
            raise ForwardRestricted(str(exc)) from exc
        except _TARGET_ERRORS as exc:
            raise TargetUnavailable(f"{target}: {exc}") from exc
        return _ids(sent)

    async def resend_from_relay(
        self, target: int, relay_ids: list[int], caption: str
    ) -> list[int]:
        messages = await self._client.get_messages(self._owner_id, ids=relay_ids)
        media = [m.media for m in messages if m is not None and m.media is not None]
        try:
            if not media:
                sent = await self._client.send_message(
                    target, caption, silent=True, link_preview=False
                )
            else:
                sent = await self._client.send_file(target, media, caption=caption, silent=True)
        except _TARGET_ERRORS as exc:
            raise TargetUnavailable(f"{target}: {exc}") from exc
        return _ids(sent)

    async def delete_relay(self, relay_ids: list[int]) -> None:
        try:
            await self._client.delete_messages(self._owner_id, relay_ids)
        except RPCError as exc:
            logger.warning("relay cleanup failed: {}", exc)

    # ---- texts -----------------------------------------------------------------

    async def send_text(
        self,
        target: int,
        text: str,
        *,
        next_digest_id: int | None = None,
        next_label: str = "Next",
        preview: bool = False,
    ) -> int:
        try:
            msg = await self._client.send_message(
                target,
                text,
                parse_mode="html",
                link_preview=preview,
                silent=True,
                buttons=next_button(next_digest_id, next_label),
            )
        except _TARGET_ERRORS as exc:
            raise TargetUnavailable(f"{target}: {exc}") from exc
        return int(msg.id)

    async def edit_text(
        self,
        target: int,
        msg_id: int,
        text: str,
        *,
        next_digest_id: int | None = None,
        next_label: str = "Next",
    ) -> None:
        try:
            await self._client.edit_message(
                target,
                msg_id,
                text,
                parse_mode="html",
                link_preview=False,
                buttons=next_button(next_digest_id, next_label),
            )
        except MessageNotModifiedError:
            return
        except _TARGET_ERRORS as exc:
            raise TargetUnavailable(f"{target}: {exc}") from exc

    async def send_owner(self, text: str) -> None:
        try:
            await self._client.send_message(self._owner_id, text)
        except RPCError as exc:
            logger.error("cannot message owner: {}", exc)

    # ---- events ----------------------------------------------------------------

    def on_next_button(self, handler: NextHandler) -> None:
        async def _on_callback(event: Any) -> None:
            digest_id = parse_callback(event.data)
            await event.answer()
            if digest_id is not None:
                await handler(digest_id)

        self._client.add_event_handler(
            _on_callback, events.CallbackQuery(pattern=rb"^next:\d+$")
        )

    def on_owner_command(self, handler: CommandHandlerFn) -> None:
        async def _on_message(event: Any) -> None:
            parsed = parse_command(event.raw_text or "")
            if parsed is None:
                return
            command, args = parsed
            if command == "start":
                await event.delete()
                return
            reply = await handler(command, args)
            await event.respond(reply or "ok")

        self._client.add_event_handler(
            _on_message,
            events.NewMessage(from_users=[self._owner_id], incoming=True, pattern=r"^/"),
        )
```

- [ ] **Step 4: Run tests and checks**

Run: `./check.sh`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/telegrief/tg/bot.py tests/test_tg_bot.py
git commit -m "feat: telethon bot gateway

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 13: Scheduler, serve loop and CLI

**Files:**
- Create: `src/telegrief/scheduler.py`, `src/telegrief/cli.py`
- Test: `tests/test_scheduler.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `parse_run_at(value) -> tuple[int, int]`, `Heartbeat(path)` with `touch()` and `run(interval_s)`, `configure_logging(level)`, `build_app(settings, user, bot, storage) -> App`, `serve(settings) -> None`, `run_once(settings, folder, from_h, to_h) -> list[str]`; click group `main` with `login`, `run-now`, `serve`.

- [ ] **Step 1: Write the failing tests**

`tests/test_scheduler.py`:

```python
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
```

`tests/test_cli.py`:

```python
from click.testing import CliRunner

from telegrief.cli import main


def test_help_lists_commands() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for name in ("login", "run-now", "serve"):
        assert name in result.output


def test_run_now_help_shows_options() -> None:
    result = CliRunner().invoke(main, ["run-now", "--help"])
    assert result.exit_code == 0
    assert "--folder" in result.output
    assert "--from-h" in result.output
    assert "--to-h" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_scheduler.py tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write scheduler.py**

```python
"""Wiring: connect clients, build services, schedule the daily run, serve forever."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from .collector import Collector
from .commands import CommandHandler
from .config import FoldersConfig, Settings, load_folders
from .delivery import Deliverer
from .digest import DigestService
from .storage import Storage
from .targets import TargetResolver
from .tg.bot import TelethonBot
from .tg.user import TelethonUser

HEARTBEAT_INTERVAL_S = 60


def parse_run_at(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    h, m = int(hour), int(minute)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"bad RUN_AT: {value}")
    return h, m


def configure_logging(level: str) -> None:
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), enqueue=False, backtrace=False, diagnose=False)


class Heartbeat:
    def __init__(self, path: Path) -> None:
        self._path = path

    def touch(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch()

    async def run(self, interval_s: int = HEARTBEAT_INTERVAL_S) -> None:
        while True:
            self.touch()
            await asyncio.sleep(interval_s)


@dataclass
class App:
    service: DigestService
    commands: CommandHandler


def _utcnow() -> datetime:
    return datetime.now(UTC)


def build_app(settings: Settings, user: TelethonUser, bot: TelethonBot, storage: Storage) -> App:
    def folders() -> FoldersConfig:
        return load_folders(settings.folders_file)

    folders()  # fail fast on a broken folders.yaml
    user.bot_username = bot.username
    collector = Collector(user, storage)
    targets = TargetResolver(user, storage, bot.username)
    deliverer = Deliverer(user, bot, settings.max_copy_bytes)
    service = DigestService(
        user=user,
        bot=bot,
        storage=storage,
        collector=collector,
        targets=targets,
        deliverer=deliverer,
        settings=settings,
        folders=folders,
        clock=_utcnow,
    )
    commands = CommandHandler(service, user, storage, collector, targets, folders, _utcnow)
    return App(service=service, commands=commands)


async def _connect(settings: Settings) -> tuple[TelethonUser, TelethonBot, Storage]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    storage = await Storage.open(settings.db_path)
    user = await TelethonUser.connect(
        settings.user_session, settings.tg_api_id, settings.tg_api_hash, settings.tmp_dir
    )
    bot = await TelethonBot.connect(
        settings.bot_session,
        settings.tg_api_id,
        settings.tg_api_hash,
        settings.bot_token,
        owner_id=user.me_id,
    )
    return user, bot, storage


async def run_once(
    settings: Settings, folder: str | None, from_h: int | None, to_h: int | None
) -> list[str]:
    configure_logging(settings.log_level)
    user, bot, storage = await _connect(settings)
    try:
        app = build_app(settings, user, bot, storage)
        if folder is None:
            return await app.service.run_all(from_h, to_h)
        cfg = load_folders(settings.folders_file).get(folder)
        if cfg is None:
            return [f"unknown folder: {folder}"]
        return [await app.service.run_folder(cfg, from_h, to_h)]
    finally:
        await bot.disconnect()
        await user.disconnect()
        await storage.close()


async def serve(settings: Settings) -> None:
    configure_logging(settings.log_level)
    user, bot, storage = await _connect(settings)
    app = build_app(settings, user, bot, storage)
    app.service.bind()
    bot.on_owner_command(app.commands.handle)
    await user.ping_bot(bot.username)
    await app.service.catch_up()

    hour, minute = parse_run_at(settings.run_at)
    tz = ZoneInfo(settings.tz)
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        app.service.run_all,
        CronTrigger(hour=hour, minute=minute, timezone=tz),
        id="daily",
        misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info("serving; daily run at {} {}", settings.run_at, settings.tz)
    try:
        await asyncio.gather(
            Heartbeat(settings.heartbeat_path).run(),
            user.run_until_disconnected(),
            bot.run_until_disconnected(),
        )
    except Exception as exc:
        logger.critical("fatal: {}", exc)
        await bot.send_owner(f"💀 telegrief stopped: {exc}")
        raise
    finally:
        scheduler.shutdown(wait=False)
        await storage.close()
```

- [ ] **Step 4: Write cli.py**

```python
"""Console entry point: telegrief login | run-now | serve."""

from __future__ import annotations

import asyncio

import click

from .config import Settings


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """telegrief — daily ranked digest of Telegram channel posts."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(serve)


@main.command()
def login() -> None:
    """Interactive one-time login for the user session."""
    from .tg.user import login as tg_login

    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(tg_login(settings.user_session, settings.tg_api_id, settings.tg_api_hash))


@main.command("run-now")
@click.option("--folder", default=None, help="Only this folder (default: all).")
@click.option("--from-h", type=int, default=None, help="Window start, hours ago.")
@click.option("--to-h", type=int, default=None, help="Window end, hours ago.")
def run_now(folder: str | None, from_h: int | None, to_h: int | None) -> None:
    """Run one digest cycle and exit. Do not use while `serve` is running."""
    from .scheduler import run_once

    for line in asyncio.run(run_once(Settings(), folder, from_h, to_h)):
        click.echo(line)


@main.command()
def serve() -> None:
    """Run forever: daily schedule, read tracking, owner commands."""
    from .scheduler import serve as serve_forever

    asyncio.run(serve_forever(Settings()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run tests and checks**

Run: `./check.sh`
Expected: PASS. `Settings()` is only constructed inside commands, so `--help` works without env vars.

- [ ] **Step 6: Commit**

```bash
git add src/telegrief/scheduler.py src/telegrief/cli.py tests/test_scheduler.py tests/test_cli.py
git commit -m "feat: scheduler, serve loop and CLI

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 14: Docker, compose, example config, README

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `docker-compose.override.yml.example`, `folders.yaml.example`, `update.sh`
- Modify: `README.md`

**Interfaces:**
- Produces: image `ghcr.io/ffoxin/telegrief` running `telegrief serve`; compose service `app`.

- [ ] **Step 1: Write Dockerfile and .dockerignore**

`Dockerfile`:

```dockerfile
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

RUN mkdir -p /app/data
VOLUME ["/app/data"]

CMD ["telegrief", "serve"]
```

`.dockerignore`:

```
.git
.venv
.data
secrets
tests
docs
.github
.mypy_cache
.ruff_cache
.pytest_cache
__pycache__
*.session*
folders.yaml
docker-compose.override.yml
```

- [ ] **Step 2: Write compose files**

`docker-compose.yml`:

```yaml
services:
  app:
    image: ghcr.io/ffoxin/telegrief:latest
    restart: unless-stopped
    environment:
      - TZ=UTC
      - RUN_AT=02:00
      - LOG_LEVEL=INFO
    secrets:
      - tg_api_id
      - tg_api_hash
      - bot_token
    volumes:
      - ./.data:/app/data
      - ./folders.yaml:/app/folders.yaml:ro
    healthcheck:
      test:
        [
          "CMD",
          "sh",
          "-c",
          "test $$(( $$(date +%s) - $$(stat -c %Y /app/data/heartbeat) )) -lt 300",
        ]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 60s
    logging:
      driver: "json-file"
      options:
        max-size: "5m"
        max-file: "5"

secrets:
  tg_api_id:
    file: ./secrets/tg_api_id.txt
  tg_api_hash:
    file: ./secrets/tg_api_hash.txt
  bot_token:
    file: ./secrets/bot_token.txt
```

`docker-compose.override.yml.example`:

```yaml
# cp docker-compose.override.yml.example docker-compose.override.yml
# to build the image locally instead of pulling from GHCR.
services:
  app:
    build: .
    image: telegrief:local
```

`folders.yaml.example`:

```yaml
folders:
  - name: Memes            # Telegram folder title, exact match
    batch_size: 10         # posts per batch (default 10)
    scoring: per_view      # raw | per_subscriber | per_view (default per_view)
    min_denominator: 200   # floor for views / subscribers (default 200)
    max_items: 1000        # digest length cap (default 1000)
  - name: News
    scoring: per_subscriber
    min_denominator: 1000
    # target: -1001234567890   # optional: existing channel where the bot is admin
```

- [ ] **Step 3: Fetch update.sh**

Run:

```bash
wget -O update.sh 'https://gist.githubusercontent.com/ffoxin/338ccd4ec811d96d7d557a7eac108077/raw/update.sh' && chmod +x update.sh
```

Expected: the script starts with `#!/usr/bin/env bash` and contains `docker compose pull`.

- [ ] **Step 4: Write README.md**

```markdown
# telegrief

Daily ranked digest of Telegram channel posts from your folders. Every day at
`RUN_AT` (default 02:00 UTC) it collects posts published in the window
`[now-48h, now-24h)` from every channel inside each configured Telegram folder,
ranks them by engagement (reactions per view by default), and forwards the
best ones into a private digest channel it creates for you. The first batch
arrives immediately; the next batch arrives once you have read the previous
one.

Design: `docs/superpowers/specs/2026-10-15-telegrief-design.md`.

## Requirements

- A Telegram **user account** (the service reads folders and channels as you;
  the Bot API cannot). Get `api_id` / `api_hash` at https://my.telegram.org.
- A bot token from @BotFather (the bot posts into digest channels and talks
  to you).
- Docker with compose.

## Setup

```bash
git clone git@github.com:ffoxin/telegrief.git && cd telegrief
mkdir -p secrets .data
echo 123456 > secrets/tg_api_id.txt
echo your_api_hash > secrets/tg_api_hash.txt
echo 123456:bot_token > secrets/bot_token.txt
cp folders.yaml.example folders.yaml   # edit: folder titles exactly as in Telegram
docker compose pull
docker compose run --rm -it app telegrief login   # phone, code, 2FA once
docker compose up -d
```

Then open the bot chat and send `/folders` to verify channel detection, and
`/run Memes 24 0` for a test digest of the last 24 hours.

## Bot commands (owner only)

- `/run [folder] [from_h] [to_h]` — run now; defaults `48 24`.
- `/status` — latest digest per folder.
- `/folders` — channels resolved per folder.
- `/reset <folder>` — forget the digest channel; next run creates a new one.

## Configuration

Environment (compose `environment:` or `/run/secrets/<name>` files):
`TG_API_ID`, `TG_API_HASH`, `BOT_TOKEN`, `TZ` (UTC), `RUN_AT` (02:00),
`WINDOW_FROM_H` (48), `WINDOW_TO_H` (24), `AUTO_NEXT_MIN_INTERVAL_S` (120),
`MAX_COPY_MB` (200), `LOG_LEVEL` (INFO).

`folders.yaml`: see `folders.yaml.example`. Re-read on every run.

## Development

```bash
uv sync
./check.sh          # ruff, mypy, pytest
./check.sh lint     # autofix
cp docker-compose.override.yml.example docker-compose.override.yml  # local image
```

## Update on the server

```bash
./update.sh
```
```

- [ ] **Step 5: Build the image locally and smoke test**

Run:

```bash
docker build -t telegrief:local . && docker run --rm telegrief:local telegrief --help
```

Expected: build succeeds; output lists `login`, `run-now`, `serve`.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile .dockerignore docker-compose.yml docker-compose.override.yml.example folders.yaml.example update.sh README.md
git commit -m "chore: docker image, compose, example config, readme

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 15: GitHub Actions CI and first push

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: `check` job (ruff, mypy, pytest) on every push/PR; `image` job pushing `ghcr.io/ffoxin/telegrief:{latest,sha-…,tag}` for `linux/amd64,linux/arm64` on push to `main` and tags.

- [ ] **Step 1: Write the workflow**

`.github/workflows/ci.yml`:

```yaml
name: ci

on:
  push:
    branches: [main]
    tags: ["v*"]
  pull_request:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          version: "0.13.1"
      - run: uv sync --frozen
      - run: uv run ruff format --check .
      - run: uv run ruff check .
      - run: uv run mypy
      - run: uv run pytest

  image:
    needs: check
    if: github.event_name == 'push'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-qemu-action@v3
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - id: meta
        uses: docker/metadata-action@v5
        with:
          images: ghcr.io/${{ github.repository }}
          tags: |
            type=raw,value=latest,enable={{is_default_branch}}
            type=sha,prefix=sha-
            type=ref,event=tag
      - uses: docker/build-push-action@v6
        with:
          context: .
          platforms: linux/amd64,linux/arm64
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

- [ ] **Step 2: Commit and push**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: lint, test and multi-arch image to GHCR

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push origin main
```

- [ ] **Step 3: Watch the run**

Run: `gh run watch --exit-status` (or `gh run list --limit 1` then `gh run view <id> --log-failed`).
Expected: both jobs green; `gh api /user/packages/container/telegrief` shows the package.

- [ ] **Step 4: Make the package public (one-time, manual)**

In GitHub: Packages → `telegrief` → Package settings → Change visibility → Public. Then on the server `docker compose pull` needs no login. Verify with `docker pull ghcr.io/ffoxin/telegrief:latest` from a machine without GHCR credentials.

---

## Self-review notes

- Spec coverage: §4 config → Task 2; §5.2 collector → Tasks 6 and 11; §5.3 scoring → Task 3; §5.4 storage → Task 4 (adds `footer_text`, `noforwards`, `preview`, `engagement`, `views` columns the orchestrator needs; `posts.grouped_id` instead of `grouped_ids`); §5.5 targets → Task 7; §5.6 orchestrator → Task 9; §5.7 messages → Tasks 8 and 9; §5.8 commands → Tasks 10 and 12; §5.9 CLI → Task 13; §7 Docker/CI → Tasks 14 and 15; §8 tests → each task.
- Deviation from spec layout: migrations live in `src/telegrief/migrations/` (packaged with the wheel) instead of a top-level `migrations/`.
- Manual verification after the first real `serve`: `edit_text` with `buttons=None` must remove the inline keyboard from the previous footer (Telegram drops the markup when `reply_markup` is omitted on edit). If the old button survives, make `next_button(None, …)` in `tg/bot.py` return `types.ReplyInlineMarkup(rows=[])` instead of `None` (not `Button.clear()`, which targets reply keyboards).
