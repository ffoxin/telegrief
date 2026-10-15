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

    async def resend_from_relay(self, target: int, relay_ids: list[int], caption: str) -> list[int]:
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
