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
