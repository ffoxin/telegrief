from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegrief.metrics import Family, MetricsWriter, collect, render
from telegrief.models import FolderConfig
from telegrief.storage import Storage

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    s = await Storage.open(tmp_path / "t.db")
    yield s
    await s.close()


def test_render_writes_help_type_and_escaped_labels() -> None:
    families = [
        Family("a_total", "counter", "Things.", [({"folder": 'm"E\\m', "b": "x"}, 3.0)]),
        Family("b", "gauge", "No labels.", [({}, 1.5)]),
    ]
    assert render(families) == (
        "# HELP a_total Things.\n"
        "# TYPE a_total counter\n"
        'a_total{b="x",folder="m\\"E\\\\m"} 3\n'
        "# HELP b No labels.\n"
        "# TYPE b gauge\n"
        "b 1.5\n"
    )


async def samples(storage: Storage) -> dict[str, float]:
    text = render(await collect(storage, NOW))
    return {
        line.rsplit(" ", 1)[0]: float(line.rsplit(" ", 1)[1])
        for line in text.splitlines()
        if not line.startswith("#")
    }


async def test_counters_exist_at_zero_before_the_first_event(storage: Storage) -> None:
    await storage.create_folder("😊mEm", FolderConfig(), NOW)
    seen = await samples(storage)
    assert seen['telegrief_fetch_posts_total{folder="😊mEm",outcome="added"}'] == 0
    assert seen['telegrief_read_posts_total{folder="😊mEm"}'] == 0
    assert seen['telegrief_indexed_posts_total{result="done"}'] == 0
    assert seen["telegrief_index_pauses_total"] == 0
    assert seen['telegrief_queue_posts{folder="😊mEm",state="pending"}'] == 0
    assert seen['telegrief_queue_posts{folder="😊mEm",state="duplicate"}'] == 0
    assert seen['telegrief_duplicate_posts_total{folder="😊mEm",match="media"}'] == 0
    assert seen['telegrief_index_posts{state="failed"}'] == 0
    assert seen["telegrief_metrics_timestamp_seconds"] == NOW.timestamp()


async def test_counters_add_up_and_follow_the_current_folders(storage: Storage) -> None:
    folder = await storage.create_folder("Memes", FolderConfig(), NOW)
    await storage.count("telegrief_fetch_posts_total", 7, folder="Memes", outcome="added")
    await storage.count("telegrief_fetch_posts_total", 5, folder="Memes", outcome="added")
    await storage.count("telegrief_fetch_posts_total", 3, folder="Gone", outcome="added")
    await storage.count("telegrief_indexed_posts_total", result="done")
    folder.last_fetch_at = NOW - timedelta(hours=1)
    folder.last_error = "boom"
    await storage.update_folder_state(folder)

    seen = await samples(storage)
    assert seen['telegrief_fetch_posts_total{folder="Memes",outcome="added"}'] == 12
    assert not any("Gone" in key for key in seen)  # a removed folder is not exported
    assert seen['telegrief_indexed_posts_total{result="done"}'] == 1
    assert seen['telegrief_folder_fetch_failing{folder="Memes"}'] == 1
    assert (
        seen['telegrief_folder_timestamp_seconds{event="last_fetch",folder="Memes"}']
        == (NOW - timedelta(hours=1)).timestamp()
    )
    assert 'telegrief_folder_timestamp_seconds{event="last_batch",folder="Memes"}' not in seen


async def test_writer_replaces_the_file_atomically(storage: Storage, tmp_path: Path) -> None:
    path = tmp_path / "telegrief.prom"
    path.write_text("stale")
    await MetricsWriter(storage, path, clock=lambda: NOW).write()
    assert path.read_text().startswith("# HELP telegrief_fetches_total")
    assert path.read_text().endswith(
        f"telegrief_metrics_timestamp_seconds {int(NOW.timestamp())}\n"
    )
    assert [p.name for p in tmp_path.iterdir() if p.suffix != ".db" and "t.db" not in p.name] == [
        "telegrief.prom"
    ]
