"""Admin web page: browse the search index, see what was read from a post, re-index it.

There is no login here. The page trusts the ``Remote-User`` header, which the
authenticating proxy in front (Authelia behind Traefik) sets; without it every
request is refused, so the port must not be reachable any other way.
"""

from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from aiohttp import web
from loguru import logger

from .delivery import bare_id, post_link
from .indexer import Indexer, fold
from .models import ChannelInfo, IndexEntry, IndexState
from .storage import Storage

PAGE_SIZE = 50
EXCERPT_CHARS = 280
USER_HEADER = "Remote-User"
FILTERS = {
    "all": "All",
    "speech": "With speech",
    "pictures": "With pictures",
    "nothing": "Nothing found",
    "pending": "Waiting",
    "failed": "Failed",
}

CSS = """
:root { color-scheme: light dark; --bg:#f6f7f9; --card:#fff; --ink:#1c2024; --dim:#667085;
  --line:#e3e6ea; --accent:#2563eb; --ok:#15803d; --warn:#b45309; --bad:#b91c1c; }
@media (prefers-color-scheme: dark) { :root { --bg:#111418; --card:#1a1e24; --ink:#e6e8eb;
  --dim:#98a2b3; --line:#2a3038; --accent:#7aa7ff; --ok:#4ade80; --warn:#fbbf24; --bad:#f87171; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:1000px; margin:0 auto; padding:16px; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
header { display:flex; flex-wrap:wrap; gap:8px 16px; align-items:baseline; margin-bottom:12px; }
h1 { font-size:20px; margin:0; } .who { color:var(--dim); margin-left:auto; font-size:13px; }
.stats { display:flex; flex-wrap:wrap; gap:6px 18px; color:var(--dim); font-size:13px;
  margin-bottom:12px; } .stats b { color:var(--ink); font-variant-numeric:tabular-nums; }
form.search { display:flex; gap:8px; margin-bottom:10px; }
input[type=search] { flex:1; min-width:0; padding:8px 10px; border:1px solid var(--line);
  border-radius:8px; background:var(--card); color:var(--ink); font:inherit; }
button { padding:8px 14px; border:1px solid var(--line); border-radius:8px; background:var(--card);
  color:var(--ink); font:inherit; cursor:pointer; } button:hover { border-color:var(--accent); }
.tabs { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:14px; }
.tabs a { padding:4px 10px; border:1px solid var(--line); border-radius:999px; font-size:13px;
  color:var(--dim); background:var(--card); }
.tabs a.on { color:var(--card); background:var(--accent); border-color:var(--accent); }
.post { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:12px 14px; margin-bottom:10px; overflow-wrap:anywhere; }
.meta { display:flex; flex-wrap:wrap; gap:4px 12px; font-size:13px; color:var(--dim);
  margin-bottom:6px; } .meta a.open { font-weight:600; }
.part { margin:6px 0 0; white-space:pre-wrap; } .part .tag { color:var(--dim); font-size:12px;
  text-transform:uppercase; letter-spacing:.04em; display:block; }
.none { color:var(--dim); font-style:italic; }
.badge { padding:0 7px; border-radius:999px; font-size:12px; border:1px solid currentColor; }
.done { color:var(--ok); } .pending { color:var(--warn); } .failed { color:var(--bad); }
.pager { display:flex; gap:14px; justify-content:center; margin:16px 0; color:var(--dim); }
.error { color:var(--bad); }
"""


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= EXCERPT_CHARS else flat[:EXCERPT_CHARS] + "…"


def _url(path: str, **params: str | int) -> str:
    query = {k: v for k, v in params.items() if v not in ("", "all", 1)}
    return f"{path}?{urlencode(query)}" if query else path


def _page(title: str, user: str, body: str) -> web.Response:
    document = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{_esc(title)}</title><style>{CSS}</style></head><body><main>"
        f"<header><h1><a href='/'>telegrief</a> · search index</h1>"
        f"<span class=who>{_esc(user)}</span></header>{body}</main></body></html>"
    )
    return web.Response(text=document, content_type="text/html")


def _placeholder(channel_id: int) -> ChannelInfo:
    return ChannelInfo(
        id=channel_id,
        username=None,
        title=str(channel_id),
        subscribers=0,
        reactions_enabled=True,
        noforwards=False,
    )


def _part(tag: str, text: str, *, full: bool) -> str:
    if not text:
        return ""
    shown = text if full else _excerpt(text)
    return f"<p class=part><span class=tag>{tag}</span>{_esc(shown)}</p>"


def render_entry(entry: IndexEntry, channel: ChannelInfo, tz: ZoneInfo, *, full: bool) -> str:
    when = entry.date.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    meta = [
        f"<a class=open href='/post/{entry.id}'>#{entry.id}</a>",
        f"<span class='badge {entry.state}'>{entry.state}</span>",
        _esc(when),
        _esc(channel.display),
        _esc(entry.folder),
        f"<a href='{_esc(post_link(channel, entry.msg_id))}' rel=noreferrer>original</a>",
    ]
    if entry.sent_msg_id is not None:
        digest = f"https://t.me/c/{bare_id(entry.target)}/{entry.sent_msg_id}"
        meta.append(f"<a href='{digest}' rel=noreferrer>in digest</a>")
    parts = (
        _part("text", entry.text, full=full)
        + _part("🖼 pictures", entry.media_text, full=full)
        + _part("🎙 speech", entry.speech_text, full=full)
    )
    if not parts:
        waiting = entry.state != IndexState.DONE
        parts = f"<p class='part none'>{'not indexed yet' if waiting else 'nothing found'}</p>"
    if entry.error and (full or entry.state == IndexState.FAILED):
        parts += f"<p class='part error'>attempts {entry.attempts}: {_esc(entry.error)}</p>"
    return f"<article class=post><div class=meta>{' · '.join(meta)}</div>{parts}</article>"


class Admin:
    def __init__(self, storage: Storage, indexer: Indexer, tz: ZoneInfo) -> None:
        self._storage = storage
        self._indexer = indexer
        self._tz = tz
        self._channels: dict[int, ChannelInfo] = {}

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._require_user])
        app.add_routes(
            [
                web.get("/", self.index),
                web.get(r"/post/{id:\d+}", self.post),
                web.post(r"/post/{id:\d+}/reindex", self.reindex),
            ]
        )
        return app

    @web.middleware
    async def _require_user(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        if not request.headers.get(USER_HEADER):
            raise web.HTTPUnauthorized(text="sign in through the proxy")
        if request.method == "POST":
            # Same-site forms only: another site cannot make the browser re-index things.
            origin = request.headers.get("Origin", "")
            if origin and origin.split("://", 1)[-1] != request.host:
                raise web.HTTPForbidden(text="cross-site request")
        return await handler(request)

    async def _channel(self, channel_id: int) -> ChannelInfo:
        known = self._channels.get(channel_id)
        if known is None:
            cached = await self._storage.get_channel(channel_id)
            known = cached[0] if cached is not None else _placeholder(channel_id)
            self._channels[channel_id] = known
        return known

    async def _stats(self) -> str:
        counts = await self._storage.index_counts()
        found = await self._storage.index_coverage()
        cells = [
            ("indexed", counts[IndexState.DONE]),
            ("with text", found["text"]),
            ("with pictures", found["pictures"]),
            ("with speech", found["speech"]),
            ("nothing found", found["nothing"]),
            ("waiting", counts[IndexState.PENDING]),
            ("failed", counts[IndexState.FAILED]),
        ]
        return (
            "<div class=stats>"
            + "".join(f"<span><b>{n}</b> {label}</span>" for label, n in cells)
            + "</div>"
        )

    async def index(self, request: web.Request) -> web.Response:
        query = request.query.get("q", "").strip()
        show = request.query.get("show", "all")
        if show not in FILTERS:
            show = "all"
        try:
            page = max(1, int(request.query.get("page", "1")))
        except ValueError:
            page = 1
        needles = [n for n in (fold(word) for word in query.split()) if n]
        entries, total = await self._storage.index_list(
            needles, show, PAGE_SIZE, (page - 1) * PAGE_SIZE
        )
        tabs = "".join(
            f"<a class='{'on' if key == show else ''}' href='{_esc(_url('/', q=query, show=key))}'>"
            f"{label}</a>"
            for key, label in FILTERS.items()
        )
        hidden = f"<input type=hidden name=show value='{show}'>" if show != "all" else ""
        body = [
            await self._stats(),
            f"<form class=search method=get action='/'>{hidden}<input type=search name=q "
            f"value='{_esc(query)}' placeholder='words, as in /find' autofocus>"
            "<button>Search</button></form>",
            f"<div class=tabs>{tabs}</div>",
        ]
        for entry in entries:
            channel = await self._channel(entry.channel_id)
            body.append(render_entry(entry, channel, self._tz, full=False))
        if not entries:
            body.append("<p class=none>no posts</p>")
        pages = max(1, -(-total // PAGE_SIZE))
        pager = [f"<span>{total} posts · page {page} of {pages}</span>"]
        if page > 1:
            pager.insert(
                0, f"<a href='{_esc(_url('/', q=query, show=show, page=page - 1))}'>← newer</a>"
            )
        if page < pages:
            pager.append(
                f"<a href='{_esc(_url('/', q=query, show=show, page=page + 1))}'>older →</a>"
            )
        body.append(f"<div class=pager>{''.join(pager)}</div>")
        return _page("telegrief · search index", request.headers[USER_HEADER], "".join(body))

    async def _entry(self, request: web.Request) -> IndexEntry:
        entry = await self._storage.index_get(int(request.match_info["id"]))
        if entry is None:
            raise web.HTTPNotFound(text="no such post")
        return entry

    async def post(self, request: web.Request) -> web.Response:
        entry = await self._entry(request)
        channel = await self._channel(entry.channel_id)
        note = "<p class=none>queued for re-indexing</p>" if "queued" in request.query else ""
        body = (
            render_entry(entry, channel, self._tz, full=True)
            + note
            + f"<form method=post action='/post/{entry.id}/reindex'>"
            "<button>Re-index this post</button></form>"
        )
        return _page(f"telegrief · #{entry.id}", request.headers[USER_HEADER], body)

    async def reindex(self, request: web.Request) -> web.Response:
        entry = await self._entry(request)
        await self._storage.index_requeue(entry.id)
        self._indexer.wake()
        logger.info("admin: {} re-indexes #{}", request.headers[USER_HEADER], entry.id)
        raise web.HTTPSeeOther(f"/post/{entry.id}?queued=1")

    async def run(self, port: int) -> None:
        runner = web.AppRunner(self.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        logger.info("admin page on port {}", port)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
