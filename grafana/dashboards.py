#!/usr/bin/env python3
"""Generate grafana/dashboards/telegrief.json (datasource uid ``infra-metrics``).

The JSON is what Grafana provisions; edit this file and regenerate rather than
editing the JSON:  python3 grafana/dashboards.py
The metrics come from src/telegrief/metrics.py through node-exporter's textfile
collector, so the datasource is the one that scrapes node-exporter.
"""

import json
from pathlib import Path

OUT = Path(__file__).parent / "dashboards" / "telegrief.json"
DS = {"type": "prometheus", "uid": "infra-metrics"}
F = 'folder=~"$folder"'
# Bars cover at least an hour: fetches and batches are hours apart.
BAR_STEP = "1h"

_id = 0


def nid() -> int:
    global _id
    _id += 1
    return _id


def tgt(expr: str, legend: str = "", ref: str = "A", instant: bool = False) -> dict:
    target = {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref}
    if instant:
        target.update({"instant": True, "range": False})
    return target


def increase(metric: str, by: str) -> str:
    """Events per bar, summed by ``by``."""
    return f"sum by ({by}) (increase({metric}[$__interval]))"


def row(title: str, y: int) -> dict:
    return {
        "type": "row",
        "id": nid(),
        "title": title,
        "collapsed": False,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
    }


def stat(title, expr, x, y, w=4, unit="short", desc="", color="blue", thresholds=None) -> dict:
    steps = [{"color": color, "value": None}]
    for step_color, value in thresholds or ():
        steps.append({"color": step_color, "value": value})
    return {
        "type": "stat",
        "id": nid(),
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": {"h": 4, "w": w, "x": x, "y": y},
        "targets": [tgt(expr, instant=True)],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "noValue": "0",
                "thresholds": {"mode": "absolute", "steps": steps},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value",
            "graphMode": "none",
            "textMode": "auto",
        },
    }


def series(title, targets, x, y, w=12, h=8, bars=False, stack=False, desc="", unit="short") -> dict:
    custom = {
        "lineWidth": 1,
        "showPoints": "never",
        "axisSoftMin": 0,
        "fillOpacity": 60 if bars else 15,
        "drawStyle": "bars" if bars else "line",
        "stacking": {"mode": "normal" if stack else "none"},
    }
    panel = {
        "type": "timeseries",
        "id": nid(),
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": targets,
        "fieldConfig": {
            "defaults": {"unit": unit, "decimals": 0, "custom": custom},
            "overrides": [],
        },
        "options": {
            "legend": {
                "displayMode": "table",
                "placement": "bottom",
                "calcs": ["sum"] if bars else ["lastNotNull", "max"],
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }
    if bars:
        panel["interval"] = BAR_STEP
    return panel


def table(title, targets, x, y, w, h, desc="") -> dict:
    return {
        "type": "table",
        "id": nid(),
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": [dict(t, format="table") for t in targets],
        "transformations": [
            {"id": "joinByField", "options": {"byField": "folder", "mode": "outer"}},
            {
                "id": "organize",
                "options": {
                    "excludeByName": {
                        "Time": True,
                        "Time 1": True,
                        "Time 2": True,
                        "Time 3": True,
                        "Time 4": True,
                        "Time 5": True,
                    },
                    "renameByName": {
                        "folder": "Folder",
                        "Value #A": "Last fetch",
                        "Value #B": "Next fetch",
                        "Value #C": "Last batch",
                        "Value #D": "Waiting",
                        "Value #E": "Fetch failing",
                    },
                },
            },
        ],
        "fieldConfig": {
            "defaults": {"custom": {"align": "auto"}},
            "overrides": [
                {
                    "matcher": {"id": "byRegexp", "options": "Last fetch|Next fetch|Last batch"},
                    "properties": [{"id": "unit", "value": "dateTimeFromNow"}],
                },
                {
                    "matcher": {"id": "byName", "options": "Fetch failing"},
                    "properties": [
                        {
                            "id": "mappings",
                            "value": [
                                {
                                    "type": "value",
                                    "options": {
                                        "0": {"text": "no", "color": "green"},
                                        "1": {"text": "YES", "color": "red"},
                                    },
                                }
                            ],
                        },
                        {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                    ],
                },
            ],
        },
        "options": {"showHeader": True},
    }


def stamp(event: str) -> str:
    return f'max by (folder) (telegrief_folder_timestamp_seconds{{{F},event="{event}"}}) * 1000'


def build() -> dict:
    panels: list[dict] = []

    # ---- now ----------------------------------------------------------------
    panels += [
        stat(
            "Waiting in the pool",
            f'sum(telegrief_queue_posts{{{F},state="pending"}})',
            0,
            0,
            desc="Posts fetched and not delivered yet.",
        ),
        stat(
            "Delivered, unread",
            f'sum(telegrief_queue_posts{{{F},state="unread"}})',
            4,
            0,
            desc="Posts in the digest channels your read marker has not passed.",
            color="orange",
        ),
        stat(
            "Indexed",
            'telegrief_index_posts{state="done"}',
            8,
            0,
            color="green",
            desc="Posts /find can return.",
        ),
        stat(
            "Index waiting",
            'telegrief_index_posts{state="pending"}',
            12,
            0,
            desc="Delivered posts not indexed yet (includes ones being retried).",
        ),
        stat(
            "Index failed",
            'telegrief_index_posts{state="failed"}',
            16,
            0,
            color="green",
            thresholds=[("red", 1)],
            desc="Given up after 4 attempts; /reindex retries them.",
        ),
        stat(
            "Metrics age",
            "time() - telegrief_metrics_timestamp_seconds",
            20,
            0,
            unit="s",
            color="green",
            thresholds=[("orange", 120), ("red", 600)],
            desc="Seconds since telegrief last wrote its metrics; large means it is down.",
        ),
        table(
            "Folders",
            [
                tgt(stamp("last_fetch"), ref="A", instant=True),
                tgt(stamp("next_fetch"), ref="B", instant=True),
                tgt(stamp("last_batch"), ref="C", instant=True),
                tgt(
                    f'sum by (folder) (telegrief_queue_posts{{{F},state="pending"}})',
                    ref="D",
                    instant=True,
                ),
                tgt(
                    f"max by (folder) (telegrief_folder_fetch_failing{{{F}}})",
                    ref="E",
                    instant=True,
                ),
            ],
            0,
            4,
            24,
            5,
        ),
    ]

    # ---- pool ---------------------------------------------------------------
    y = 9
    panels += [
        row("Pool: what each fetch brings", y),
        series(
            "Posts added to the pool per fetch",
            [
                tgt(
                    increase(f'telegrief_fetch_posts_total{{{F},outcome="added"}}', "folder"),
                    "{{folder}}",
                )
            ],
            0,
            y + 1,
            bars=True,
            stack=True,
            desc="New posts each fetch put into the queue. One bar is one hour, so a bar is "
            "one fetch unless two folders fetch in the same hour (they stack).",
        ),
        series(
            "What fetches collected",
            [tgt(increase(f"telegrief_fetch_posts_total{{{F}}}", "outcome"), "{{outcome}}")],
            12,
            y + 1,
            bars=True,
            stack=True,
            desc="added: new in the queue. known: already queued or delivered. "
            "skipped: dropped by a skip phrase.",
        ),
        series(
            "Pool size",
            [tgt(f'sum by (folder) (telegrief_queue_posts{{{F},state="pending"}})', "{{folder}}")],
            0,
            y + 9,
            desc="Posts waiting for delivery. Drops are batches going out and "
            "posts ageing past max_age.",
        ),
        series(
            "Fetches",
            [tgt(increase(f"telegrief_fetches_total{{{F}}}", "result"), "{{result}}")],
            12,
            y + 9,
            bars=True,
            stack=True,
            desc="Fetch runs, successful and failed.",
        ),
    ]

    # ---- reading ------------------------------------------------------------
    y = 27
    panels += [
        row("Reading: what was delivered and read, and when", y),
        series(
            "Posts read",
            [tgt(increase(f"telegrief_read_posts_total{{{F}}}", "folder"), "{{folder}}")],
            0,
            y + 1,
            bars=True,
            stack=True,
            desc="Delivered posts your read marker passed, per hour.",
        ),
        series(
            "Posts delivered",
            [tgt(increase(f"telegrief_delivered_posts_total{{{F}}}", "delivery"), "{{delivery}}")],
            12,
            y + 1,
            bars=True,
            stack=True,
            desc="Posts sent to the digest channels, by how: forward, copy (protected "
            "source), link (fallback), failed (not delivered).",
        ),
        series(
            "Delivered and not read yet",
            [tgt(f'sum by (folder) (telegrief_queue_posts{{{F},state="unread"}})', "{{folder}}")],
            0,
            y + 9,
            desc="The backlog in the digest channels.",
        ),
        series(
            "Batches",
            [tgt(increase(f"telegrief_batches_total{{{F}}}", "trigger"), "{{trigger}}")],
            12,
            y + 9,
            bars=True,
            stack=True,
            desc="auto: sent because the previous batch was read. manual: the Next button.",
        ),
    ]

    # ---- index --------------------------------------------------------------
    y = 45
    panels += [
        row("Search index: how indexing is going", y),
        series(
            "Index entries by state",
            [tgt("telegrief_index_posts", "{{state}}")],
            0,
            y + 1,
            stack=True,
            desc="done: searchable. pending: waiting or being retried. failed: given up.",
        ),
        series(
            "Indexing attempts",
            [tgt(increase("telegrief_indexed_posts_total", "result"), "{{result}}")],
            12,
            y + 1,
            bars=True,
            stack=True,
            desc="done: indexed. retry: failed, will be tried again in 10 min. "
            "failed: 4th failure, given up.",
        ),
        series(
            "Pictures and speech",
            [tgt(increase("telegrief_index_media_total", "kind, result"), "{{kind}} {{result}}")],
            0,
            y + 9,
            bars=True,
            stack=True,
            desc="Claude calls for pictures (one per post) and Groq calls for sound clips. "
            "rejected: the service refused that input; the post is indexed without it.",
        ),
        series(
            "Rate-limit pauses",
            [tgt("sum(increase(telegrief_index_pauses_total[$__interval]))", "pauses")],
            12,
            y + 9,
            w=6,
            bars=True,
            desc="Indexing passes stopped by an Anthropic or Groq rate limit. "
            "Indexing resumes by itself within 10 minutes.",
        ),
        series(
            "What indexed posts contain",
            [tgt("telegrief_index_posts_with", "{{content}}")],
            18,
            y + 9,
            w=6,
            desc="Indexed posts with their own text, with a picture description, "
            "with a transcript.",
        ),
    ]

    return {
        "uid": "telegrief-overview",
        "title": "telegrief",
        "tags": ["telegrief"],
        "schemaVersion": 39,
        "editable": False,
        "refresh": "1m",
        "time": {"from": "now-7d", "to": "now"},
        "timezone": "browser",
        "templating": {
            "list": [
                {
                    "name": "folder",
                    "label": "Folder",
                    "type": "query",
                    "datasource": DS,
                    "query": {
                        "query": "label_values(telegrief_queue_posts, folder)",
                        "refId": "folder",
                    },
                    "refresh": 2,
                    "includeAll": True,
                    "multi": True,
                    "allValue": ".*",
                    "current": {"selected": True, "text": "All", "value": "$__all"},
                    "sort": 1,
                }
            ]
        },
        "panels": panels,
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
