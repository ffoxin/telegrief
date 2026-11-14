"""Metrics as a Prometheus text file, for node-exporter's textfile collector.

Events are counted in the database (``Storage.count``) so the counters survive
restarts; current sizes are read from the tables when the file is written.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from .models import Delivery, IndexState, ItemState

if TYPE_CHECKING:
    from .storage import Storage

WRITE_INTERVAL_S = 30.0

# name -> (help, label sets written as 0 for every folder until the event first happens,
# so a series exists before its first increase)
FOLDER_COUNTERS: dict[str, tuple[str, list[dict[str, str]]]] = {
    "telegrief_fetches_total": (
        "Fetches of a folder, by result.",
        [{"result": "ok"}, {"result": "error"}],
    ),
    "telegrief_fetch_posts_total": (
        "Posts a fetch collected: added to the queue, already known, or dropped by a skip phrase.",
        [{"outcome": "added"}, {"outcome": "known"}, {"outcome": "skipped"}],
    ),
    "telegrief_batches_total": (
        "Batches sent to the digest channel, by trigger.",
        [{"trigger": "auto"}, {"trigger": "manual"}],
    ),
    "telegrief_delivered_posts_total": (
        "Posts sent to the digest channel, by how (failed: not delivered).",
        [{"delivery": d} for d in (*Delivery, "failed")],
    ),
    "telegrief_read_posts_total": ("Delivered posts the owner's read marker has passed.", [{}]),
}
GLOBAL_COUNTERS: dict[str, tuple[str, list[dict[str, str]]]] = {
    "telegrief_indexed_posts_total": (
        "Indexing attempts: done, retry (will be tried again), failed (given up).",
        [{"result": "done"}, {"result": "retry"}, {"result": "failed"}],
    ),
    "telegrief_index_media_total": (
        "Pictures described (one call per post) and sound clips transcribed, by result.",
        [{"kind": k, "result": r} for k in ("pictures", "speech") for r in ("ok", "rejected")],
    ),
    "telegrief_index_pauses_total": ("Indexing passes cut short by a rate limit.", [{}]),
}


@dataclass
class Family:
    name: str
    kind: str  # counter | gauge
    help: str
    samples: list[tuple[dict[str, str], float]] = field(default_factory=list)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value: float) -> str:
    return str(int(value)) if value == int(value) else repr(value)


def render(families: list[Family]) -> str:
    lines: list[str] = []
    for family in families:
        lines.append(f"# HELP {family.name} {family.help}")
        lines.append(f"# TYPE {family.name} {family.kind}")
        for labels, value in family.samples:
            body = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
            lines.append(
                f"{family.name}{{{body}}} {_number(value)}"
                if body
                else f"{family.name} {_number(value)}"
            )
    return "\n".join(lines) + "\n"


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def collect(storage: Storage, now: datetime) -> list[Family]:
    """Every metric: the stored counters (for current folders) and gauges from the tables."""
    folders = await storage.list_folders()
    names = {f.name for f in folders}
    stored: dict[str, dict[str, float]] = {}
    for name, labels, value in await storage.counters():
        if "folder" in labels and labels["folder"] not in names:
            continue  # a removed folder: its history stays in the database only
        stored.setdefault(name, {})[_key(labels)] = value

    families: list[Family] = []

    def counter(name: str, text: str, label_sets: list[dict[str, str]]) -> None:
        family = Family(name, "counter", text)
        values = stored.get(name, {})
        seen: set[str] = set()
        for labels in label_sets:
            seen.add(_key(labels))
            family.samples.append((labels, values.get(_key(labels), 0.0)))
        for key, value in values.items():  # label sets not listed above
            if key not in seen:
                family.samples.append((_unkey(key), value))
        families.append(family)

    for name, (text, label_sets) in FOLDER_COUNTERS.items():
        counter(name, text, [{"folder": f.name, **ls} for f in folders for ls in label_sets])
    for name, (text, label_sets) in GLOBAL_COUNTERS.items():
        counter(name, text, label_sets)

    queue = await storage.queue_counts()
    states = (ItemState.PENDING, "unread", ItemState.SENT, ItemState.FAILED)
    families.append(
        Family(
            "telegrief_queue_posts",
            "gauge",
            "Queue rows: pending (waiting), unread (delivered, not read), sent (read), failed.",
            [
                ({"folder": f.name, "state": str(s)}, float(queue.get((f.id, str(s)), 0)))
                for f in folders
                for s in states
            ],
        )
    )
    stamps = Family(
        "telegrief_folder_timestamp_seconds",
        "gauge",
        "When a folder was last fetched, last sent a batch, and is fetched next.",
    )
    errors = Family("telegrief_folder_fetch_failing", "gauge", "1 while the last fetch failed.")
    for f in folders:
        for event, when in (
            ("last_fetch", f.last_fetch_at),
            ("last_batch", f.last_batch_at),
            ("next_fetch", f.next_run_at),
        ):
            if when is not None:
                stamps.samples.append(({"folder": f.name, "event": event}, when.timestamp()))
        errors.samples.append(({"folder": f.name}, float(f.last_error is not None)))
    families += [stamps, errors]

    index = await storage.index_counts()
    families.append(
        Family(
            "telegrief_index_posts",
            "gauge",
            "Search index entries by state.",
            [({"state": str(s)}, float(index[s])) for s in IndexState],
        )
    )
    coverage = await storage.index_coverage()
    families.append(
        Family(
            "telegrief_index_posts_with",
            "gauge",
            "Indexed posts that have text, a picture description, a transcript.",
            [({"content": k}, float(v)) for k, v in coverage.items()],
        )
    )
    families.append(
        Family(
            "telegrief_metrics_timestamp_seconds",
            "gauge",
            "When this file was written.",
            [({}, now.timestamp())],
        )
    )
    return families


def _key(labels: dict[str, str]) -> str:
    return "\x00".join(f"{k}\x01{v}" for k, v in sorted(labels.items()))


def _unkey(key: str) -> dict[str, str]:
    return dict(pair.split("\x01", 1) for pair in key.split("\x00")) if key else {}


class MetricsWriter:
    def __init__(
        self, storage: Storage, path: Path, clock: Callable[[], datetime] = _utcnow
    ) -> None:
        self._storage = storage
        self._path = path
        self._clock = clock

    async def write(self) -> None:
        text = render(await collect(self._storage, self._clock()))
        # node-exporter reads only *.prom, so the temporary file is invisible to it
        tmp = self._path.with_name(f".{self._path.name}.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.chmod(0o644)
        os.replace(tmp, self._path)

    async def run(self, interval_s: float = WRITE_INTERVAL_S) -> None:
        while True:
            try:
                await self.write()
            except Exception:
                logger.exception("cannot write metrics to {}", self._path)
            await asyncio.sleep(interval_s)
