"""Telethon user-session gateway: reading, channel creation, relay uploads."""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from telethon import TelegramClient, events, utils
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyInvalidError,
    AuthKeyUnregisteredError,
    ChatForwardsRestrictedError,
    RPCError,
    SessionExpiredError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.tl import functions, types

from ..config import normalize_title
from ..media import speech_clip
from ..models import ChannelInfo, Image, ImageType, PostContent, RawPost
from .protocols import (
    PING_TEXT,
    ChannelUnavailable,
    CopyFailed,
    FolderNotFound,
    ForwardRestricted,
    GatewayError,
    MediaTooLarge,
    ReadHandler,
    SessionDead,
)

PREVIEW_CHARS = 80
CAPTION_LIMIT = 1024
FLOOD_SLEEP_THRESHOLD = 120
# Search index: photos are read at the smallest size that keeps text legible, image
# files only when small, and the sound of a video or audio file only when the file is.
PHOTO_MIN_SIDE = 1200
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_SPEECH_BYTES = 50 * 1024 * 1024
IMAGE_MIMES: dict[str, ImageType] = {
    "image/jpeg": "image/jpeg",
    "image/png": "image/png",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
}
FATAL_ERRORS = (
    AuthKeyUnregisteredError,
    AuthKeyInvalidError,
    AuthKeyDuplicatedError,
    SessionExpiredError,
    SessionRevokedError,
    UserDeactivatedError,
    UserDeactivatedBanError,
)


def _translate_fatal[**P, R](
    fn: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """Turn Telethon's 'session is gone' errors into SessionDead."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except FATAL_ERRORS as exc:
            raise SessionDead(str(exc)) from exc

    return wrapper


def filter_title(dialog_filter: Any) -> str:
    title = getattr(dialog_filter, "title", "")
    return str(getattr(title, "text", title))


def add_pinned_peer(dialog_filter: Any, peer: Any) -> bool:
    """Prepend ``peer`` to the folder's pinned and included peers.

    Returns False when the channel is already in the folder (pinned or included).
    """
    peer_id = utils.get_peer_id(peer)
    present = set(channel_peer_ids(dialog_filter.pinned_peers))
    present.update(channel_peer_ids(dialog_filter.include_peers))
    if peer_id in present:
        return False
    dialog_filter.pinned_peers.insert(0, peer)
    dialog_filter.include_peers.insert(0, peer)
    return True


def remove_peer(dialog_filter: Any, peer_id: int) -> bool:
    """Drop the channel with marked id ``peer_id`` from pinned and included peers."""

    def keep(peers: list[Any]) -> list[Any]:
        return [
            p
            for p in peers
            if not (isinstance(p, types.InputPeerChannel) and utils.get_peer_id(p) == peer_id)
        ]

    pinned, included = keep(dialog_filter.pinned_peers), keep(dialog_filter.include_peers)
    changed = len(pinned) != len(dialog_filter.pinned_peers) or len(included) != len(
        dialog_filter.include_peers
    )
    dialog_filter.pinned_peers, dialog_filter.include_peers = pinned, included
    return changed


def match_folder(filters: Iterable[Any], folder_name: str) -> Any:
    """Pick the dialog filter for ``folder_name``: exact title first, then normalized.

    Raises FolderNotFound when nothing matches or several titles normalize alike.
    """
    candidates = list(filters)
    for dialog_filter in candidates:
        if filter_title(dialog_filter) == folder_name:
            return dialog_filter
    wanted = normalize_title(folder_name)
    matches = [f for f in candidates if normalize_title(filter_title(f)) == wanted]
    if len(matches) == 1:
        return matches[0]
    if matches:
        titles = " · ".join(filter_title(f) for f in matches)
        raise FolderNotFound(f"{folder_name}: ambiguous: {titles}")
    raise FolderNotFound(folder_name)


def select_channel_ids(
    *,
    include: Iterable[int],
    pinned: Iterable[int],
    exclude: Iterable[int],
    broadcasts: bool,
    all_broadcasts: Iterable[int],
) -> list[int]:
    chosen: dict[int, None] = {}
    for channel_id in (*pinned, *include):
        chosen.setdefault(channel_id, None)
    if broadcasts:
        for channel_id in all_broadcasts:
            chosen.setdefault(channel_id, None)
    for channel_id in exclude:
        chosen.pop(channel_id, None)
    return list(chosen)


def is_broadcast(entity: Any) -> bool:
    return isinstance(entity, types.Channel) and bool(entity.broadcast) and not entity.megagroup


def channel_peer_ids(peers: Iterable[Any]) -> list[int]:
    """Marked ids of the channel peers only; users, chats and Saved Messages are dropped."""
    return [utils.get_peer_id(p) for p in peers if isinstance(p, types.InputPeerChannel)]


def compose_caption(head: str, text: str) -> str:
    caption = f"{head}\n\n{text}" if text else head
    return caption[:CAPTION_LIMIT]


def raw_post_from_message(msg: Any, channel_id: int) -> RawPost | None:
    if getattr(msg, "action", None) is not None or msg.date is None:
        return None
    reactions = 0
    if getattr(msg, "reactions", None) is not None:
        reactions = sum(int(r.count) for r in msg.reactions.results)
    replies_obj = getattr(msg, "replies", None)
    replies = int(replies_obj.replies) if replies_obj is not None else 0
    text = " ".join((msg.message or "").split())
    return RawPost(
        channel_id=channel_id,
        msg_id=int(msg.id),
        grouped_id=msg.grouped_id,
        date=msg.date,
        views=int(msg.views or 0),
        forwards=int(msg.forwards or 0),
        reactions=reactions,
        replies=replies,
        preview=text[:PREVIEW_CHARS],
        noforwards=bool(getattr(msg, "noforwards", False)),
        text=text,
    )


def pick_photo_size(sizes: Iterable[Any]) -> Any:
    """Smallest size with a side of PHOTO_MIN_SIDE or more, else the largest; None if none."""
    sized = sorted(
        (s for s in sizes if isinstance(s, types.PhotoSize | types.PhotoSizeProgressive)),
        key=lambda s: max(s.w, s.h),
    )
    if not sized:
        return None
    return next((s for s in sized if max(s.w, s.h) >= PHOTO_MIN_SIDE), sized[-1])


def has_sound(document: Any) -> bool:
    """A video or audio file that can carry speech: not a GIF, sticker or muted video."""
    mime = document.mime_type or ""
    if not mime.startswith(("video/", "audio/")):
        return False
    for attr in document.attributes:
        if isinstance(attr, types.DocumentAttributeAnimated | types.DocumentAttributeSticker):
            return False
        if isinstance(attr, types.DocumentAttributeVideo) and attr.nosound:
            return False
    return True


def _ids(messages: Any) -> list[int]:
    items = messages if isinstance(messages, list) else [messages]
    return [int(m.id) for m in items if m is not None]


class TelethonUser:
    def __init__(self, client: TelegramClient, me_id: int, tmp_dir: Path) -> None:
        self._client = client
        self.me_id = me_id
        self._tmp = tmp_dir
        self.bot_username = ""

    @classmethod
    async def connect(
        cls, session: Path, api_id: int, api_hash: str, tmp_dir: Path
    ) -> TelethonUser:
        client = TelegramClient(
            str(session), api_id, api_hash, flood_sleep_threshold=FLOOD_SLEEP_THRESHOLD
        )
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("user session is not authorized; run `telegrief login` first")
        me = await client.get_me()
        logger.info("user session: {} (id {})", me.first_name, me.id)
        return cls(client, int(me.id), tmp_dir)

    async def run_until_disconnected(self) -> None:
        await self._client.run_until_disconnected()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    # ---- folders -------------------------------------------------------------

    async def _dialog_filters(self) -> list[Any]:
        result = await self._client(functions.messages.GetDialogFiltersRequest())
        filters = getattr(result, "filters", result)
        return [
            f for f in filters if isinstance(f, types.DialogFilter | types.DialogFilterChatlist)
        ]

    @_translate_fatal
    async def list_folder_titles(self) -> list[str]:
        return [filter_title(f) for f in await self._dialog_filters()]

    @_translate_fatal
    async def folder_title(self, folder_name: str) -> str:
        return filter_title(match_folder(await self._dialog_filters(), folder_name))

    @_translate_fatal
    async def folder_channels(self, folder_name: str) -> list[int]:
        dialog_filter = match_folder(await self._dialog_filters(), folder_name)
        include = [p for p in dialog_filter.include_peers if isinstance(p, types.InputPeerChannel)]
        pinned = [p for p in dialog_filter.pinned_peers if isinstance(p, types.InputPeerChannel)]
        exclude = list(getattr(dialog_filter, "exclude_peers", []))
        peers = {utils.get_peer_id(p): p for p in (*pinned, *include)}
        all_broadcasts: list[int] = []
        if getattr(dialog_filter, "broadcasts", False):
            async for dialog in self._client.iter_dialogs():
                if is_broadcast(dialog.entity):
                    all_broadcasts.append(int(dialog.id))
        ids = select_channel_ids(
            include=channel_peer_ids(include),
            pinned=channel_peer_ids(pinned),
            exclude=channel_peer_ids(exclude),
            broadcasts=bool(getattr(dialog_filter, "broadcasts", False)),
            all_broadcasts=all_broadcasts,
        )
        channels: list[int] = []
        for channel_id in ids:
            if await self._is_broadcast_channel(peers.get(channel_id, channel_id)):
                channels.append(channel_id)
        return channels

    async def _is_broadcast_channel(self, peer: Any) -> bool:
        try:
            entity = await self._client.get_entity(peer)
        except (ValueError, RPCError) as exc:
            logger.warning("cannot resolve {}: {}", peer, exc)
            return False
        return is_broadcast(entity)

    # ---- channels and posts --------------------------------------------------

    @_translate_fatal
    async def channel_info(self, channel_id: int) -> ChannelInfo:
        try:
            entity = await self._client.get_entity(channel_id)
            full = await self._client(functions.channels.GetFullChannelRequest(entity))
        except FATAL_ERRORS:
            raise
        except (RPCError, ValueError) as exc:
            raise ChannelUnavailable(str(exc)) from exc
        full_chat = full.full_chat
        return ChannelInfo(
            id=utils.get_peer_id(entity),
            username=entity.username,
            title=entity.title,
            subscribers=int(full_chat.participants_count or 0),
            reactions_enabled=not isinstance(
                full_chat.available_reactions, types.ChatReactionsNone
            ),
            noforwards=bool(entity.noforwards),
        )

    async def iter_posts(
        self, channel_id: int, start: datetime, end: datetime
    ) -> AsyncIterator[RawPost]:
        try:
            async for msg in self._client.iter_messages(channel_id, offset_date=end):
                if msg.date < start:
                    break
                post = raw_post_from_message(msg, channel_id)
                if post is not None:
                    yield post
        except FATAL_ERRORS as exc:
            raise SessionDead(str(exc)) from exc
        except (RPCError, ValueError) as exc:
            raise ChannelUnavailable(str(exc)) from exc

    # ---- digest channel ------------------------------------------------------

    @_translate_fatal
    async def ensure_in_folder(self, folder_name: str, channel_id: int) -> bool:
        dialog_filter = match_folder(await self._dialog_filters(), folder_name)
        peer = await self._client.get_input_entity(channel_id)
        if not add_pinned_peer(dialog_filter, peer):
            return False
        try:
            await self._client(
                functions.messages.UpdateDialogFilterRequest(
                    id=dialog_filter.id, filter=dialog_filter
                )
            )
        except RPCError as exc:
            raise GatewayError(f"cannot update folder {folder_name!r}: {exc}") from exc
        return True

    @_translate_fatal
    async def remove_from_folder(self, folder_name: str, channel_id: int) -> bool:
        dialog_filter = match_folder(await self._dialog_filters(), folder_name)
        if not remove_peer(dialog_filter, channel_id):
            return False
        try:
            await self._client(
                functions.messages.UpdateDialogFilterRequest(
                    id=dialog_filter.id, filter=dialog_filter
                )
            )
        except RPCError as exc:
            raise GatewayError(f"cannot update folder {folder_name!r}: {exc}") from exc
        return True

    @_translate_fatal
    async def create_digest_channel(self, title: str, bot_username: str) -> int:
        result = await self._client(
            functions.channels.CreateChannelRequest(
                title=title, about="telegrief digest", broadcast=True
            )
        )
        channel = result.chats[0]
        bot = await self._client.get_input_entity(bot_username)
        try:
            await self._client(functions.channels.InviteToChannelRequest(channel, [bot]))
        except RPCError as exc:  # bots are often addable only via EditAdmin
            logger.debug("invite bot: {}", exc)
        await self._client(
            functions.channels.EditAdminRequest(
                channel,
                bot,
                types.ChatAdminRights(post_messages=True, edit_messages=True, delete_messages=True),
                rank="digest",
            )
        )
        return int(utils.get_peer_id(channel))

    # ---- relay ---------------------------------------------------------------

    @_translate_fatal
    async def forward_to_relay(self, channel_id: int, msg_ids: list[int]) -> list[int]:
        try:
            sent = await self._client.forward_messages(
                self.bot_username, msg_ids, from_peer=channel_id, silent=True
            )
        except ChatForwardsRestrictedError as exc:
            raise ForwardRestricted(str(exc)) from exc
        return _ids(sent)

    @_translate_fatal
    async def copy_to_relay(
        self, channel_id: int, msg_ids: list[int], caption_head: str, max_bytes: int
    ) -> list[int]:
        messages = await self._client.get_messages(channel_id, ids=msg_ids)
        messages = [m for m in messages if m is not None]
        if not messages:
            raise CopyFailed("source messages not found")
        total = sum(int(m.file.size or 0) for m in messages if m.file is not None)
        if total > max_bytes:
            raise MediaTooLarge(f"{total} bytes > {max_bytes}")
        text = next((m.message for m in messages if m.message), "")
        caption = compose_caption(caption_head, text)
        self._tmp.mkdir(parents=True, exist_ok=True)
        files: list[str] = []
        try:
            for m in messages:
                if m.media is None or isinstance(m.media, types.MessageMediaWebPage):
                    continue
                path = await self._client.download_media(
                    m, file=str(self._tmp / f"{abs(channel_id)}_{m.id}")
                )
                if path:
                    files.append(str(path))
            if not files:
                sent = await self._client.send_message(
                    self.bot_username, caption, silent=True, link_preview=False
                )
            else:
                sent = await self._client.send_file(
                    self.bot_username, files, caption=caption, silent=True
                )
        except (RPCError, OSError) as exc:
            raise CopyFailed(str(exc)) from exc
        finally:
            for path in files:
                Path(path).unlink(missing_ok=True)
        return _ids(sent)

    # ---- search index --------------------------------------------------------

    @_translate_fatal
    async def post_content(
        self, channel_id: int, msg_ids: list[int], *, images: bool, speech: bool
    ) -> PostContent:
        try:
            messages = await self._client.get_messages(channel_id, ids=msg_ids)
            messages = [m for m in messages if m is not None]
            if not messages:
                raise ChannelUnavailable("post not found")
            pictures: list[Image] = []
            clips: list[bytes] = []
            for m in messages:
                image = await self._image(m) if images else None
                if image is not None:
                    pictures.append(image)
                clip = await self._speech(m, channel_id) if speech else None
                if clip is not None:
                    clips.append(clip)
        except FATAL_ERRORS:
            raise
        except (RPCError, ValueError, OSError) as exc:
            raise ChannelUnavailable(str(exc)) from exc
        text = " ".join(" ".join(m.message.split()) for m in messages if m.message)
        return PostContent(text=text, images=tuple(pictures), speech=tuple(clips))

    async def _image(self, msg: Any) -> Image | None:
        if msg.media is None or isinstance(msg.media, types.MessageMediaWebPage):
            return None
        if msg.photo is not None:
            size = pick_photo_size(msg.photo.sizes)
            data = await self._client.download_media(msg, bytes, thumb=size)
            return Image(data, "image/jpeg") if data else None
        document = msg.document
        if document is None or document.mime_type not in IMAGE_MIMES:
            return None
        if int(document.size or 0) > MAX_IMAGE_BYTES:
            return None
        data = await self._client.download_media(msg, bytes)
        return Image(data, IMAGE_MIMES[document.mime_type]) if data else None

    async def _speech(self, msg: Any, channel_id: int) -> bytes | None:
        """Sound of a video, round video, voice message or audio file, cut for transcription."""
        if msg.media is None or isinstance(msg.media, types.MessageMediaWebPage):
            return None
        document = msg.document
        if document is None or not has_sound(document):
            return None
        if int(document.size or 0) > MAX_SPEECH_BYTES:
            return None
        self._tmp.mkdir(parents=True, exist_ok=True)
        path = self._tmp / f"speech_{abs(channel_id)}_{msg.id}"
        try:
            if not await self._client.download_media(msg, file=str(path)):
                return None
            return await speech_clip(path)
        finally:
            for leftover in self._tmp.glob(f"{path.name}*"):
                leftover.unlink(missing_ok=True)

    # ---- read tracking -------------------------------------------------------

    @_translate_fatal
    async def read_marker(self, channel_id: int) -> int:
        peer = await self._client.get_input_entity(channel_id)
        result = await self._client(
            functions.messages.GetPeerDialogsRequest(peers=[types.InputDialogPeer(peer)])
        )
        return int(result.dialogs[0].read_inbox_max_id) if result.dialogs else 0

    def on_channel_read(self, handler: ReadHandler) -> None:
        async def _on_update(update: types.UpdateReadChannelInbox) -> None:
            marked = utils.get_peer_id(types.PeerChannel(update.channel_id))
            await handler(int(marked), int(update.max_id))

        self._client.add_event_handler(_on_update, events.Raw(types.UpdateReadChannelInbox))

    @_translate_fatal
    async def ping_bot(self, bot_username: str) -> None:
        await self._client.send_message(bot_username, PING_TEXT)


async def login(session: Path, api_id: int, api_hash: str) -> None:
    """Interactive one-time login (phone, code, 2FA) creating the session file."""
    client = TelegramClient(str(session), api_id, api_hash)
    await client.start()
    me = await client.get_me()
    print(f"logged in as {me.first_name} (id {me.id}); session saved to {session}.session")
    await client.disconnect()
