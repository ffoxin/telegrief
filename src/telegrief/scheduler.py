"""Wiring: connect clients, build services, schedule the daily run, serve forever."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
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


def build_app(
    settings: Settings,
    user: TelethonUser,
    bot: TelethonBot,
    storage: Storage,
    on_fatal: Callable[[Exception], None] | None = None,
) -> App:
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
        on_fatal=on_fatal,
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
    stop = asyncio.Event()
    fatal: list[Exception] = []

    def on_fatal(exc: Exception) -> None:
        fatal.append(exc)
        stop.set()

    app = build_app(settings, user, bot, storage, on_fatal=on_fatal)
    app.service.bind()
    bot.on_owner_command(app.commands.handle)
    await user.ping_bot(bot.username)
    await app.service.catch_up()

    async def daily() -> None:
        try:
            await app.service.run_all()
        except Exception as exc:
            logger.exception("daily run failed")
            await bot.send_owner(f"❌ daily run failed: {exc}")

    hour, minute = parse_run_at(settings.run_at)
    tz = ZoneInfo(settings.tz)
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        daily,
        CronTrigger(hour=hour, minute=minute, timezone=tz),
        id="daily",
        misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info("serving; daily run at {} {}", settings.run_at, settings.tz)
    tasks = [
        asyncio.create_task(Heartbeat(settings.heartbeat_path).run(), name="heartbeat"),
        asyncio.create_task(user.run_until_disconnected(), name="user"),
        asyncio.create_task(bot.run_until_disconnected(), name="bot"),
        asyncio.create_task(stop.wait(), name="fatal"),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        reason = "user session dead; run `telegrief login`" if fatal else "client disconnected"
        for task in done:
            if task.get_name() != "fatal" and task.exception() is not None:
                reason = f"{task.get_name()}: {task.exception()}"
        logger.critical("stopping: {}", reason)
        await bot.send_owner(f"💀 telegrief stopped: {reason}")
        raise SystemExit(1)
    finally:
        scheduler.shutdown(wait=False)
        await storage.close()
