"""Telethon bot gateway: delivery into digest channels, owner chat, buttons."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from loguru import logger
from telethon import Button, TelegramClient, events
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChatForwardsRestrictedError,
    ChatWriteForbiddenError,
    MessageNotModifiedError,
    RPCError,
)
from telethon.tl import functions, types

from .protocols import (
    PING_TEXT,
    CommandHandlerFn,
    ForwardRestricted,
    GatewayError,
    NextHandler,
    RelayTimeout,
    TargetUnavailable,
)

FLOOD_SLEEP_THRESHOLD = 120
REPLY_LIMIT = 4000
# Prefix differs from the pre-queue "next:<digest_id>" so stale buttons stay inert.
_CALLBACK = re.compile(rb"^queue:(\d+)$")
_COMMAND = re.compile(r"^/([a-zA-Z_]+)(?:@\w+)?(?:\s+(.*))?$", re.DOTALL)
_TARGET_ERRORS = (ChannelPrivateError, ChatAdminRequiredError, ChatWriteForbiddenError, ValueError)


def parse_callback(data: bytes | None) -> int | None:
    if not data:
        return None
    match = _CALLBACK.match(data)
    return int(match.group(1)) if match else None


def parse_command(text: str) -> tuple[str, list[str]] | None:
    match = _COMMAND.match(text.strip()) if text else None
    if not match:
        return None
    return match.group(1).lower(), (match.group(2) or "").split()


async def dispatch_owner_message(event: Any, handler: CommandHandlerFn) -> None:
    """Route one owner message: startup ping is deleted, commands get a reply."""
    parsed = parse_command(event.raw_text or "")
    if parsed is None:
        return
    command, args = parsed
    if command == PING_TEXT.lstrip("/"):
        await event.delete()
        return
    try:
        reply = await handler(command, args)
    except Exception as exc:
        logger.exception("command /{} failed", command)
        reply = f"❌ {exc}"
    await event.respond((reply or "ok")[:REPLY_LIMIT])


def next_button(folder_id: int | None, label: str) -> Any:
    if folder_id is None:
        return None
    return Button.inline(label, data=f"queue:{folder_id}".encode())


def _ids(messages: Any) -> list[int]:
    items = messages if isinstance(messages, list) else [messages]
    return [int(m.id) for m in items if m is not None]


def relay_media(messages: list[Any]) -> list[Any]:
    """Media objects worth re-sending; link previews are not media."""
    return [
        m.media
        for m in messages
        if m is not None
        and m.media is not None
        and not isinstance(m.media, types.MessageMediaWebPage)
    ]


def _is_relay_message(msg: Any) -> bool:
    if msg.fwd_from is not None or msg.media is not None:
        return True
    return not (msg.message or "").startswith("/")


class TelethonBot:
    def __init__(self, client: TelegramClient, username: str, owner_id: int) -> None:
        self._client = client
        self.username = username
        self._owner_id = owner_id
        # Relay inbox: ids (in the bot's own id-space) of messages the owner sent us.
        self._inbox: list[int] = []
        self._inbox_changed = asyncio.Event()
        self._client.add_event_handler(
            self._track_relay, events.NewMessage(from_users=[owner_id], incoming=True)
        )

    async def _track_relay(self, event: Any) -> None:
        msg = event.message
        if _is_relay_message(msg):
            self._inbox.append(int(msg.id))
            self._inbox_changed.set()

    def clear_relay(self) -> None:
        self._inbox.clear()
        self._inbox_changed.clear()

    async def await_relay(self, count: int, timeout: float) -> list[int]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self._inbox) < count:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RelayTimeout(f"expected {count} relay messages, got {len(self._inbox)}")
            self._inbox_changed.clear()
            try:
                await asyncio.wait_for(self._inbox_changed.wait(), remaining)
            except TimeoutError:
                continue
        ids, self._inbox = self._inbox[:count], self._inbox[count:]
        return ids

    @classmethod
    async def connect(
        cls, session: Path, api_id: int, api_hash: str, token: str, owner_id: int
    ) -> TelethonBot:
        client = TelegramClient(
            str(session), api_id, api_hash, flood_sleep_threshold=FLOOD_SLEEP_THRESHOLD
        )
        await client.start(bot_token=token)
        me = await client.get_me()
        logger.info("bot session: @{}", me.username)
        return cls(client, str(me.username), owner_id)

    async def run_until_disconnected(self) -> None:
        await self._client.run_until_disconnected()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    # ---- delivery --------------------------------------------------------------

    async def forward_from_relay(self, target: int, relay_ids: list[int]) -> list[int]:
        try:
            sent = await self._client.forward_messages(
                target, relay_ids, from_peer=self._owner_id, silent=True
            )
        except ChatForwardsRestrictedError as exc:
            raise ForwardRestricted(str(exc)) from exc
        except _TARGET_ERRORS as exc:
            raise TargetUnavailable(f"{target}: {exc}") from exc
        return _ids(sent)

    async def resend_from_relay(self, target: int, relay_ids: list[int]) -> list[int]:
        messages = await self._client.get_messages(self._owner_id, ids=relay_ids)
        caption = next((m.message for m in messages if m is not None and m.message), "")
        media = relay_media(list(messages))
        try:
            if not media:
                sent = await self._client.send_message(
                    target, caption, silent=True, link_preview=False
                )
            else:
                sent = await self._client.send_file(target, media, caption=caption, silent=True)
        except _TARGET_ERRORS as exc:
            raise TargetUnavailable(f"{target}: {exc}") from exc
        return _ids(sent)

    async def delete_relay(self, relay_ids: list[int]) -> None:
        try:
            await self._client.delete_messages(self._owner_id, relay_ids)
        except RPCError as exc:
            logger.warning("relay cleanup failed: {}", exc)

    # ---- texts -----------------------------------------------------------------

    async def send_text(
        self,
        target: int,
        text: str,
        *,
        next_id: int | None = None,
        next_label: str = "Next",
        preview: bool = False,
    ) -> int:
        try:
            msg = await self._client.send_message(
                target,
                text,
                parse_mode="html",
                link_preview=preview,
                silent=True,
                buttons=next_button(next_id, next_label),
            )
        except _TARGET_ERRORS as exc:
            raise TargetUnavailable(f"{target}: {exc}") from exc
        return int(msg.id)

    async def edit_text(
        self,
        target: int,
        msg_id: int,
        text: str,
        *,
        next_id: int | None = None,
        next_label: str = "Next",
    ) -> None:
        try:
            await self._client.edit_message(
                target,
                msg_id,
                text,
                parse_mode="html",
                link_preview=False,
                buttons=next_button(next_id, next_label),
            )
        except MessageNotModifiedError:
            return
        except _TARGET_ERRORS as exc:
            raise TargetUnavailable(f"{target}: {exc}") from exc
        except RPCError as exc:
            raise GatewayError(f"{target}/{msg_id}: {exc}") from exc

    async def send_owner(self, text: str) -> None:
        try:
            await self._client.send_message(self._owner_id, text[:REPLY_LIMIT])
        except (RPCError, ValueError) as exc:
            logger.error("cannot message owner: {}", exc)

    async def set_menu(self, commands: list[tuple[str, str]], about: str) -> None:
        try:
            await self._client(
                functions.bots.SetBotCommandsRequest(
                    scope=types.BotCommandScopeDefault(),
                    lang_code="",
                    commands=[types.BotCommand(name, text) for name, text in commands],
                )
            )
            await self._client(functions.bots.SetBotInfoRequest(lang_code="", description=about))
        except RPCError as exc:
            logger.warning("cannot publish the command menu: {}", exc)

    # ---- events ----------------------------------------------------------------

    def on_next_button(self, handler: NextHandler) -> None:
        async def _on_callback(event: Any) -> None:
            if event.sender_id != self._owner_id:
                await event.answer("not for you")
                return
            folder_id = parse_callback(event.data)
            await event.answer()
            if folder_id is not None:
                await handler(folder_id)

        self._client.add_event_handler(_on_callback, events.CallbackQuery(pattern=rb"^queue:\d+$"))

    def on_owner_command(self, handler: CommandHandlerFn) -> None:
        async def _on_message(event: Any) -> None:
            await dispatch_owner_message(event, handler)

        self._client.add_event_handler(
            _on_message,
            events.NewMessage(from_users=[self._owner_id], incoming=True, pattern=r"^/"),
        )
