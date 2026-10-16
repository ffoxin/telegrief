"""Owner-only bot commands: /folder, /folders, /run, /status, /skip, /unskip, /reset."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from .collector import Collector, digest_targets
from .digest import DigestService
from .models import Folder, FolderConfig
from .scoring import format_count
from .targets import TargetResolver
from .tg.protocols import FolderNotFound, UserGateway

if TYPE_CHECKING:
    from .storage import Storage


@dataclass(frozen=True)
class Command:
    name: str
    usage: str  # syntax, starts with /name
    summary: str  # one line: overview entry and the client's command menu
    details: str = ""  # shown by /help <name>


COMMANDS: tuple[Command, ...] = (
    Command(
        "folder",
        "/folder <name> [key=value ...]",
        "add a folder or change its settings",
        "Adds the Telegram folder <name> with default settings, or changes the given ones.\n"
        "/folder <name> alone shows the current settings.\n"
        "/folder <name> off removes the folder and its queue; the digest channel stays.\n"
        "\n"
        "Keys (default in brackets):\n"
        "min_age — hours a post must be old before it is fetched [24]\n"
        "period — hours between fetches; each fetch covers one period [24]\n"
        "batch — posts per batch [10]\n"
        "scoring — per_view | per_subscriber | raw [per_view]\n"
        "min_denominator — floor for views / subscribers when scoring [200]\n"
        "max_items — queue cap, lowest scores dropped first [1000]\n"
        "max_age — days a post may wait in the queue [7]\n"
        "target — id of an existing channel to post digests to (bot must be admin)\n"
        "next — HH:MM (in TZ) or now: when the next fetch runs\n"
        "\n"
        "Examples:\n"
        "/folder Memes min_age=24 period=6\n"
        "/folder Memes batch=20 next=09:00",
    ),
    Command("folders", "/folders", "folders with settings, queue size and resolved channels"),
    Command(
        "run",
        "/run [folder] [from_h] [to_h]",
        "fetch now",
        "Fetches one folder, or every folder when none is given.\n"
        "The window defaults to the folder's min_age / period; from_h and to_h are hours "
        "before now, e.g. /run Memes 24 0 fetches the last 24 hours.",
    ),
    Command("status", "/status", "last fetch, queue size and next fetch per folder"),
    Command(
        "skip",
        "/skip [phrase]",
        "list skip phrases, or add one",
        "A post whose text contains the phrase (anywhere, any case) is never queued.\n"
        "Applies to fetches from now on; posts already queued stay.\n"
        "Example: /skip #реклама",
    ),
    Command("unskip", "/unskip <phrase>", "stop skipping a phrase"),
    Command(
        "reset",
        "/reset <folder>",
        "forget the digest channel",
        "Forgets the folder's digest channel and takes it out of the Telegram folder; "
        "the next fetch creates a new one. The old channel itself is kept.",
    ),
    Command("help", "/help [command]", "this list, or details of one command"),
)
_BY_NAME = {c.name: c for c in COMMANDS}


def help_text() -> str:
    lines = [f"{c.usage} — {c.summary}" for c in COMMANDS]
    lines.append("/help <command> shows details, e.g. /help folder")
    return "\n".join(lines)


def help_for(name: str) -> str | None:
    command = _BY_NAME.get(name.lstrip("/"))
    if command is None:
        return None
    head = f"{command.usage} — {command.summary}"
    return f"{head}\n\n{command.details}" if command.details else head


def unknown_command(name: str) -> str:
    return f"unknown command /{name.lstrip('/')}; /help lists them"


def menu() -> list[tuple[str, str]]:
    """(command, description) pairs for the client's command menu."""
    return [(c.name, c.summary) for c in COMMANDS]


USAGE = help_text()
ABOUT = (
    "Top posts from your Telegram folders, queued best-first into a digest channel. "
    "/help lists the commands."
)
NO_SKIPS = "no skip phrases; /skip <phrase> adds one"
NO_FOLDERS = "no folders configured; /folder <name> adds one"

# /folder key -> FolderConfig field
CONFIG_KEYS = {
    "min_age": "min_age_h",
    "period": "period_h",
    "batch": "batch_size",
    "scoring": "scoring",
    "min_denominator": "min_denominator",
    "max_items": "max_items",
    "max_age": "max_age_d",
}
ACTION_KEYS = frozenset({"target", "next"})
UNITS = {"min_age": "h", "period": "h", "max_age": "d"}
TIME_FMT = "%Y-%m-%d %H:%M"


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


def parse_folder_args(args: list[str]) -> tuple[str, dict[str, str], bool]:
    """Split ``/folder`` arguments into (name, key=value pairs, off flag).

    The name is every word before the first ``key=value``; a trailing ``off`` with no
    pairs removes the folder. Raises ValueError on bad syntax.
    """
    words = list(args)
    name_words: list[str] = []
    while words and "=" not in words[0]:
        name_words.append(words.pop(0))
    pairs: dict[str, str] = {}
    for word in words:
        key, _, value = word.partition("=")
        if not key or not value:
            raise ValueError(f"expected key=value, got {word}")
        pairs[key] = value
    off = False
    if len(name_words) > 1 and name_words[-1] == "off":
        if pairs:
            raise ValueError("off takes no settings")
        off = True
        name_words.pop()
    name = " ".join(name_words)
    if not name:
        raise ValueError("folder name required")
    if name == "off":
        raise ValueError("'off' is reserved")
    return name, pairs, off


def apply_pairs(base: FolderConfig, pairs: dict[str, str]) -> FolderConfig:
    """``base`` with ``pairs`` applied; ValueError names an unknown key or the first bad value."""
    unknown = sorted(set(pairs) - set(CONFIG_KEYS))
    if unknown:
        raise ValueError(f"unknown key: {', '.join(unknown)}")
    data = base.model_dump()
    data.update({CONFIG_KEYS[key]: value for key, value in pairs.items()})
    try:
        return FolderConfig.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "config"
        raise ValueError(f"{field}: {first['msg']}") from exc


def parse_target(value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"target: expected a channel id, got {value}") from exc


def next_occurrence(value: str, now: datetime, tz: ZoneInfo) -> datetime:
    """``now`` itself, or the next wall-clock ``HH:MM`` in ``tz`` strictly after now."""
    if value == "now":
        return now
    try:
        at = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"next: expected HH:MM or now, got {value}") from exc
    local = now.astimezone(tz)
    candidate = datetime.combine(local.date(), at, tzinfo=tz)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def fmt(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime(TIME_FMT)


def describe(folder: Folder, pending: int, tz: ZoneInfo) -> str:
    cfg = folder.config
    return (
        f"{folder.name} · {cfg.min_age_h}/{cfg.period_h}h · batch {cfg.batch_size} · "
        f"{cfg.scoring}/{cfg.min_denominator} · max {cfg.max_items} · {cfg.max_age_d}d · "
        f"queued {pending} · next {fmt(folder.next_run_at, tz)}"
    )


class CommandHandler:
    def __init__(
        self,
        service: DigestService,
        user: UserGateway,
        storage: Storage,
        collector: Collector,
        targets: TargetResolver,
        tz: ZoneInfo,
        clock: Callable[[], datetime],
    ) -> None:
        self._service = service
        self._user = user
        self._storage = storage
        self._collector = collector
        self._targets = targets
        self._tz = tz
        self._clock = clock

    async def handle(self, command: str, args: list[str]) -> str:
        if command == "folder":
            return await self._folder(args)
        if command == "folders":
            return await self._list_folders()
        if command == "run":
            return await self._run(args)
        if command == "status":
            return await self._status()
        if command == "reset":
            return await self._reset(args)
        if command == "skip":
            return await self._skip(args)
        if command == "unskip":
            return await self._unskip(args)
        if command in ("help", "start"):
            if not args:
                return USAGE
            return help_for(args[0]) or unknown_command(args[0])
        return unknown_command(command)

    # ---- /folder -------------------------------------------------------------

    async def _folder(self, args: list[str]) -> str:
        try:
            name, pairs, off = parse_folder_args(args)
        except ValueError as exc:
            return f"❌ {exc}"
        folder = await self._storage.find_folder(name)
        if off:
            if folder is None:
                return f"unknown folder: {name}"
            await self._storage.delete_folder(folder.id)
            return f"✅ {folder.name} removed; digest channel kept"
        settings = {k: v for k, v in pairs.items() if k not in ACTION_KEYS}
        now = self._clock()
        try:
            cfg = apply_pairs(folder.config if folder is not None else FolderConfig(), settings)
            target = parse_target(pairs["target"]) if "target" in pairs else None
            next_run = next_occurrence(pairs["next"], now, self._tz) if "next" in pairs else None
        except ValueError as exc:
            return f"❌ {exc}"
        if target is not None:
            owner = await self._storage.target_owner(target)
            if owner is not None and owner != (folder.name if folder is not None else name):
                return f"❌ channel {target} is already the digest channel of {owner}"
        if folder is None:
            return await self._create(name, cfg, target, next_run, now)
        if not pairs:
            return describe(folder, await self._storage.count_pending(folder.id), self._tz)
        if cfg != folder.config:
            await self._storage.update_folder_config(folder.id, cfg)
            if (cfg.scoring, cfg.min_denominator) != (
                folder.config.scoring,
                folder.config.min_denominator,
            ):
                await self._service.rescore(folder.id)
        note = await self._set_target(folder.name, target, now) if target is not None else ""
        if next_run is not None:
            await self._storage.set_next_run(folder.id, next_run)
        changes = [
            f"{key} {fmt(next_run, self._tz) if key == 'next' and next_run else value}"
            f"{UNITS.get(key, '')}"
            for key, value in pairs.items()
        ]
        return f"✅ {folder.name}: {', '.join(changes)}{note}"

    async def _set_target(self, name: str, target: int, now: datetime) -> str:
        """Map ``name`` to ``target``; a different old channel is unpinned via reset first."""
        old = await self._storage.get_target(name)
        note = ""
        if old is not None and old != target:
            result = await self._targets.reset(name)
            if result is not None and result.error is not None:
                note = (
                    f" (⚠️ old digest channel {old} still in the folder: {result.error}; "
                    "remove it by hand)"
                )
            elif result is not None and result.removed_from_folder:
                note = " (old digest channel removed from the folder)"
        await self._storage.set_target(name, target, now)
        return note

    async def _create(
        self,
        name: str,
        cfg: FolderConfig,
        target: int | None,
        next_run: datetime | None,
        now: datetime,
    ) -> str:
        folder = await self._storage.create_folder(name, cfg, now)
        note = await self._set_target(folder.name, target, now) if target is not None else ""
        if next_run is not None:
            await self._storage.set_next_run(folder.id, next_run)
        when = f"at {fmt(next_run, self._tz)}" if next_run is not None else "within a minute"
        return (
            f"✅ {folder.name}: every {cfg.period_h}h, posts "
            f"{cfg.min_age_h}–{cfg.min_age_h + cfg.period_h}h old, first fetch {when}{note}"
        )

    # ---- the rest ------------------------------------------------------------

    async def _run(self, args: list[str]) -> str:
        name, from_h, to_h = parse_run_args(args)
        if from_h is not None and to_h is not None and from_h <= to_h:
            return "from_h must be greater than to_h"
        if name is None:
            folders = await self._storage.list_folders()
            if not folders:
                return NO_FOLDERS
            return "\n".join(
                [await self._service.fetch(f.id, from_h=from_h, to_h=to_h) for f in folders]
            )
        folder = await self._storage.find_folder(name)
        if folder is None:
            return f"unknown folder: {name}"
        return await self._service.fetch(folder.id, from_h=from_h, to_h=to_h)

    async def _status(self) -> str:
        folders = await self._storage.list_folders()
        if not folders:
            return NO_FOLDERS
        lines: list[str] = []
        for folder in folders:
            pending = await self._storage.count_pending(folder.id)
            fetched = (
                f"fetched {fmt(folder.last_fetch_at, self._tz)}"
                if folder.last_fetch_at is not None
                else "never fetched"
            )
            if folder.fetch_added:
                fetched += f" (+{folder.fetch_added} new)"
            batch = (
                f"batch {fmt(folder.last_batch_at, self._tz)}"
                if folder.last_batch_at is not None
                else "no batch yet"
            )
            line = (
                f"{folder.name}: {fetched} · queued {pending} · {batch} · "
                f"next {fmt(folder.next_run_at, self._tz)}"
            )
            if folder.last_error:
                line += f" · ⚠️ {folder.last_error}"
            lines.append(line)
        return "\n".join(lines)

    async def _list_folders(self) -> str:
        now = self._clock()
        titles = await self._user.list_folder_titles()
        lines: list[str] = [f"Telegram folders: {' · '.join(titles) or '(none)'}"]
        folders = await self._storage.list_folders()
        if not folders:
            lines.append(NO_FOLDERS)
            return "\n".join(lines)
        exclude = await digest_targets(self._storage)
        for folder in folders:
            lines.append(describe(folder, await self._storage.count_pending(folder.id), self._tz))
            try:
                channel_ids = await self._collector.source_channels(folder.name, exclude)
            except FolderNotFound:
                lines.append("  ❌ folder not found")
                continue
            for channel_id in channel_ids:
                try:
                    info = await self._collector.channel_info(channel_id, now)
                except Exception as exc:
                    lines.append(f"  {channel_id} · ❌ {exc}")
                    continue
                lines.append(f"  {info.title} ({info.display}) · {format_count(info.subscribers)}")
        return "\n".join(lines)

    # ---- /skip, /unskip ------------------------------------------------------

    async def _skip(self, args: list[str]) -> str:
        phrase = " ".join(args).casefold()
        if not phrase:
            phrases = await self._storage.skip_phrases()
            return "\n".join(f"🚫 {p}" for p in phrases) or NO_SKIPS
        if await self._storage.add_skip(phrase):
            return f"✅ skipping {phrase}"
        return f"already skipping {phrase}"

    async def _unskip(self, args: list[str]) -> str:
        phrase = " ".join(args).casefold()
        if not phrase:
            return help_for("unskip") or USAGE
        if await self._storage.remove_skip(phrase):
            return f"✅ {phrase} no longer skipped"
        return f"not skipping {phrase}"

    async def _reset(self, args: list[str]) -> str:
        name = " ".join(args)
        if not name:
            return help_for("reset") or USAGE
        folder = await self._storage.find_folder(name)
        if folder is not None:
            name = folder.name
        result = await self._targets.reset(name)
        if result is None:
            return f"no digest channel mapping for {name}"
        if result.error is not None:
            return (
                f"reset {name}; ⚠️ old digest channel {result.channel_id} still in the folder "
                f"({result.error}); remove it by hand; next run creates a new one"
            )
        if result.removed_from_folder:
            return (
                f"reset {name}; old digest channel removed from the folder; "
                "next run creates a new one"
            )
        return f"reset {name}; next run creates a new one"
