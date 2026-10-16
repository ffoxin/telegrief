"""Wiring: connect clients, build services, tick the folder schedules, serve forever."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from loguru import logger

from .collector import Collector
from .commands import ABOUT, CommandHandler, menu
from .config import Settings
from .delivery import Deliverer
from .digest import DigestService
from .storage import Storage
from .targets import TargetResolver
from .tg.bot import TelethonBot
from .tg.user import TelethonUser

HEARTBEAT_INTERVAL_S = 60
TICK_INTERVAL_S = 60


def configure_logging(level: str) -> None:
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), enqueue=False, backtrace=False, diagnose=False)


def _utcnow() -> datetime:
    return datetime.now(UTC)


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


def latest_slot(next_run_at: datetime, period: timedelta, now: datetime) -> datetime:
    """Latest slot ``<= now`` on the grid ``next_run_at + k * period`` (k >= 0)."""
    if next_run_at > now:
        raise ValueError("not due yet")
    return next_run_at + (now - next_run_at) // period * period


class Ticker:
    """Every ``interval_s``: fetch each due folder, windowed on its latest complete slot.

    ``next_run_at`` moves to the slot after the one fetched, whether the fetch succeeded
    or not, so older missed slots are skipped and a broken folder retries next period.
    """

    def __init__(
        self,
        storage: Storage,
        service: DigestService,
        clock: Callable[[], datetime] = _utcnow,
        interval_s: float = TICK_INTERVAL_S,
    ) -> None:
        self._storage = storage
        self._service = service
        self._clock = clock
        self._interval = interval_s

    async def tick(self) -> list[str]:
        now = self._clock()
        results: list[str] = []
        for folder in await self._storage.due_folders(now):
            period = timedelta(hours=folder.config.period_h)
            slot = latest_slot(folder.next_run_at, period, now)
            try:
                results.append(await self._service.fetch(folder.id, at=slot, scheduled=True))
            except Exception:
                logger.exception("folder {}: scheduled fetch crashed", folder.name)
            finally:
                await self._storage.set_next_run(folder.id, slot + period)
        return results

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("tick failed")
            await asyncio.sleep(self._interval)


@dataclass
class App:
    service: DigestService
    commands: CommandHandler
    ticker: Ticker


def build_app(
    settings: Settings,
    user: TelethonUser,
    bot: TelethonBot,
    storage: Storage,
    on_fatal: Callable[[Exception], None] | None = None,
) -> App:
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
        clock=_utcnow,
        on_fatal=on_fatal,
    )
    commands = CommandHandler(
        service, user, storage, collector, targets, ZoneInfo(settings.tz), _utcnow
    )
    return App(service=service, commands=commands, ticker=Ticker(storage, service, _utcnow))


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
            return await app.service.fetch_all(from_h, to_h)
        found = await storage.find_folder(folder)
        if found is None:
            return [f"unknown folder: {folder}"]
        return [await app.service.fetch(found.id, from_h=from_h, to_h=to_h)]
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
    await bot.set_menu(menu(), ABOUT)
    await user.ping_bot(bot.username)
    await app.service.catch_up()
    logger.info("serving; folder schedules checked every {}s", TICK_INTERVAL_S)
    tasks = [
        asyncio.create_task(Heartbeat(settings.heartbeat_path).run(), name="heartbeat"),
        asyncio.create_task(app.ticker.run(), name="ticker"),
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
        await storage.close()
