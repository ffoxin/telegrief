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


class RelayTimeout(GatewayError):
    """A message sent to the bot's chat never showed up on the bot's side."""


class SessionDead(GatewayError):
    """The user session is no longer authorized; the operator must log in again."""


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
        self, channel_id: int, msg_ids: list[int], caption_head: str, max_bytes: int
    ) -> list[int]: ...

    async def read_marker(self, channel_id: int) -> int: ...

    def on_channel_read(self, handler: ReadHandler) -> None: ...

    async def ping_bot(self, bot_username: str) -> None: ...


@runtime_checkable
class BotGateway(Protocol):
    @property
    def username(self) -> str: ...

    def clear_relay(self) -> None: ...

    async def await_relay(self, count: int, timeout: float) -> list[int]: ...

    async def forward_from_relay(self, target: int, relay_ids: list[int]) -> list[int]: ...

    async def resend_from_relay(self, target: int, relay_ids: list[int]) -> list[int]: ...

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
