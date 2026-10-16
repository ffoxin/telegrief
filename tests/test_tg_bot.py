from types import SimpleNamespace

import pytest
from telethon.errors import MessageIdInvalidError
from telethon.tl import functions, types

from telegrief.tg.bot import (
    TelethonBot,
    dispatch_owner_message,
    next_button,
    parse_callback,
    parse_command,
    relay_media,
)
from telegrief.tg.protocols import PING_TEXT, GatewayError, RelayTimeout, TargetUnavailable


def test_parse_callback() -> None:
    assert parse_callback(b"queue:12") == 12
    assert parse_callback(b"queue:") is None
    assert parse_callback(b"other:1") is None
    assert parse_callback(None) is None


def test_parse_callback_ignores_pre_queue_digest_buttons() -> None:
    # Footers posted before migration 002 carry next:<digest_id>; those ids are not folder ids.
    assert parse_callback(b"next:12") is None
    assert next_button(5, "Next 10").type.data == b"queue:5"


def test_parse_command() -> None:
    assert parse_command("/run Memes 24 0") == ("run", ["Memes", "24", "0"])
    assert parse_command("/run@telegrief_bot Memes") == ("run", ["Memes"])
    assert parse_command("/status") == ("status", [])
    assert parse_command("hello") is None
    assert parse_command("") is None


class _Event:
    def __init__(self, text: str) -> None:
        self.raw_text = text
        self.deleted = False
        self.replies: list[str] = []

    async def delete(self) -> None:
        self.deleted = True

    async def respond(self, text: str) -> None:
        self.replies.append(text)


async def _echo(command: str, args: list[str]) -> str:
    return f"{command} {args}"


async def test_dispatch_deletes_startup_ping_silently() -> None:
    event = _Event(PING_TEXT)
    await dispatch_owner_message(event, _echo)
    assert event.deleted is True
    assert event.replies == []


async def test_dispatch_start_goes_to_handler() -> None:
    event = _Event("/start")
    await dispatch_owner_message(event, _echo)
    assert event.deleted is False
    assert event.replies == ["start []"]


async def test_dispatch_ignores_non_commands() -> None:
    event = _Event("hello")
    await dispatch_owner_message(event, _echo)
    assert event.replies == []


async def test_dispatch_reports_handler_error() -> None:
    async def boom(command: str, args: list[str]) -> str:
        raise RuntimeError("nope")

    event = _Event("/run x")
    await dispatch_owner_message(event, boom)
    assert event.replies == ["❌ nope"]


def test_next_button() -> None:
    assert next_button(None, "Next 10") is None
    button = next_button(5, "Next 10")
    assert button is not None
    assert button.type.data == b"queue:5"
    assert button.text == "Next 10"


class _FakeClient:
    def __init__(self) -> None:
        self.handlers: list[object] = []

    def add_event_handler(self, callback: object, event: object) -> None:
        self.handlers.append(callback)


class _FakeEvent:
    def __init__(self, data: bytes, sender_id: int) -> None:
        self.data = data
        self.sender_id = sender_id
        self.answers: list[str | None] = []

    async def answer(self, message: str | None = None) -> None:
        self.answers.append(message)


def _relay_event(msg_id: int, *, text: str = "", fwd: bool = True, media: object = None) -> object:
    return SimpleNamespace(
        message=SimpleNamespace(
            id=msg_id, message=text, fwd_from=object() if fwd else None, media=media
        )
    )


async def test_relay_inbox_collects_bot_side_ids() -> None:
    client = _FakeClient()
    bot = TelethonBot(client, "telegrief_bot", owner_id=1)  # type: ignore[arg-type]
    track = client.handlers[0]
    await track(_relay_event(7000))  # type: ignore[operator]
    await track(_relay_event(7001, text="/status", fwd=False))  # command, not relay
    await track(_relay_event(7002))  # type: ignore[operator]
    assert await bot.await_relay(2, timeout=0.05) == [7000, 7002]
    bot.clear_relay()
    await track(_relay_event(7003))  # type: ignore[operator]
    with pytest.raises(RelayTimeout):
        await bot.await_relay(2, timeout=0.05)


async def test_relay_inbox_waits_for_late_arrival() -> None:
    import asyncio

    client = _FakeClient()
    bot = TelethonBot(client, "telegrief_bot", owner_id=1)  # type: ignore[arg-type]
    track = client.handlers[0]

    async def arrive_later() -> None:
        await asyncio.sleep(0.01)
        await track(_relay_event(7000))  # type: ignore[operator]

    waiter = asyncio.create_task(bot.await_relay(1, timeout=1.0))
    await arrive_later()
    assert await waiter == [7000]


def test_relay_media_skips_webpage_previews() -> None:
    photo = object()
    webpage = types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=0))
    messages = [
        SimpleNamespace(media=photo),
        SimpleNamespace(media=webpage),
        None,
        SimpleNamespace(media=None),
    ]
    assert relay_media(messages) == [photo]


class _CommandEvent:
    def __init__(self, text: str) -> None:
        self.raw_text = text
        self.replies: list[str] = []

    async def respond(self, text: str) -> None:
        self.replies.append(text)

    async def delete(self) -> None:
        pass


async def test_owner_command_errors_are_reported_and_long_replies_truncated() -> None:
    client = _FakeClient()
    bot = TelethonBot(client, "telegrief_bot", owner_id=1)  # type: ignore[arg-type]

    async def handler(command: str, args: list[str]) -> str:
        if command == "boom":
            raise RuntimeError("folders.yaml is broken")
        return "x" * 10_000

    bot.on_owner_command(handler)
    on_message = client.handlers[-1]
    failing = _CommandEvent("/boom")
    await on_message(failing)  # type: ignore[operator]
    assert failing.replies == ["❌ folders.yaml is broken"]
    long = _CommandEvent("/status")
    await on_message(long)  # type: ignore[operator]
    assert len(long.replies[0]) <= 4000


async def test_next_button_ignores_non_owner() -> None:
    client = _FakeClient()
    bot = TelethonBot(client, "telegrief_bot", owner_id=1)  # type: ignore[arg-type]
    pressed: list[int] = []

    async def handler(folder_id: int) -> None:
        pressed.append(folder_id)

    bot.on_next_button(handler)
    callback = client.handlers[-1]
    stranger = _FakeEvent(b"queue:5", sender_id=999)
    await callback(stranger)  # type: ignore[operator]
    assert pressed == []
    owner = _FakeEvent(b"queue:5", sender_id=1)
    await callback(owner)  # type: ignore[operator]
    assert pressed == [5]


async def test_edit_text_wraps_other_rpc_errors() -> None:
    class Client:
        def add_event_handler(self, *args: object, **kwargs: object) -> None:
            pass

        async def edit_message(self, *args: object, **kwargs: object) -> None:
            raise MessageIdInvalidError(request=None)

    bot = TelethonBot(Client(), "bot", owner_id=1)  # type: ignore[arg-type]
    with pytest.raises(GatewayError) as info:
        await bot.edit_text(-100, 5, "x")
    assert not isinstance(info.value, TargetUnavailable)


class _MenuClient:
    def __init__(self, fail: bool = False) -> None:
        self.requests: list[object] = []
        self.fail = fail

    def add_event_handler(self, *args: object, **kwargs: object) -> None:
        pass

    async def __call__(self, request: object) -> None:
        if self.fail:
            raise MessageIdInvalidError(request=None)
        self.requests.append(request)


async def test_set_menu_registers_commands_and_description() -> None:
    client = _MenuClient()
    bot = TelethonBot(client, "bot", owner_id=1)  # type: ignore[arg-type]
    await bot.set_menu([("folder", "add a folder"), ("help", "this list")], "About text")
    commands, info = client.requests
    assert isinstance(commands, functions.bots.SetBotCommandsRequest)
    assert isinstance(commands.scope, types.BotCommandScopeDefault)
    assert [(c.command, c.description) for c in commands.commands] == [
        ("folder", "add a folder"),
        ("help", "this list"),
    ]
    assert isinstance(info, functions.bots.SetBotInfoRequest)
    assert info.description == "About text"


async def test_set_menu_failure_is_not_fatal() -> None:
    bot = TelethonBot(_MenuClient(fail=True), "bot", owner_id=1)  # type: ignore[arg-type]
    await bot.set_menu([("help", "this list")], "About text")
