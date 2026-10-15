"""Telethon bot gateway: delivery into digest channels, owner chat, buttons."""

from __future__ import annotations

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

from .protocols import CommandHandlerFn, ForwardRestricted, NextHandler, TargetUnavailable

FLOOD_SLEEP_THRESHOLD = 120
_CALLBACK = re.compile(rb"^next:(\d+)$")
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


def next_button(digest_id: int | None, label: str) -> Any:
    if digest_id is None:
        return None
    return Button.inline(label, data=f"next:{digest_id}".encode())


def _ids(messages: Any) -> list[int]:
    items = messages if isinstance(messages, list) else [messages]
    return [int(m.id) for m in items if m is not None]


class TelethonBot:
    def __init__(self, client: TelegramClient, username: str, owner_id: int) -> None:
        self._client = client
        self.username = username
        self._owner_id = owner_id

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

    async def resend_from_relay(self, target: int, relay_ids: list[int], caption: str) -> list[int]:
        messages = await self._client.get_messages(self._owner_id, ids=relay_ids)
        media = [m.media for m in messages if m is not None and m.media is not None]
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
        next_digest_id: int | None = None,
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
                buttons=next_button(next_digest_id, next_label),
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
        next_digest_id: int | None = None,
        next_label: str = "Next",
    ) -> None:
        try:
            await self._client.edit_message(
                target,
                msg_id,
                text,
                parse_mode="html",
                link_preview=False,
                buttons=next_button(next_digest_id, next_label),
            )
        except MessageNotModifiedError:
            return
        except _TARGET_ERRORS as exc:
            raise TargetUnavailable(f"{target}: {exc}") from exc

    async def send_owner(self, text: str) -> None:
        try:
            await self._client.send_message(self._owner_id, text)
        except RPCError as exc:
            logger.error("cannot message owner: {}", exc)

    # ---- events ----------------------------------------------------------------

    def on_next_button(self, handler: NextHandler) -> None:
        async def _on_callback(event: Any) -> None:
            digest_id = parse_callback(event.data)
            await event.answer()
            if digest_id is not None:
                await handler(digest_id)

        self._client.add_event_handler(_on_callback, events.CallbackQuery(pattern=rb"^next:\d+$"))

    def on_owner_command(self, handler: CommandHandlerFn) -> None:
        async def _on_message(event: Any) -> None:
            parsed = parse_command(event.raw_text or "")
            if parsed is None:
                return
            command, args = parsed
            if command == "start":
                await event.delete()
                return
            reply = await handler(command, args)
            await event.respond(reply or "ok")

        self._client.add_event_handler(
            _on_message,
            events.NewMessage(from_users=[self._owner_id], incoming=True, pattern=r"^/"),
        )
